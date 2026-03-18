"""
BTC whale wallet monitoring via mempool.space public API.

Polls configured whale wallets every 5 minutes.
Detects transfers to/from known exchange addresses.

- Whale sends BTC to exchange → bearish signal (potential sell)
- Whale withdraws BTC from exchange → bullish signal (accumulation)

API: https://mempool.space/api (no API key needed)
Rate limit: ~10 req/min on public instance → 0.5s between calls
BTC uses UTXO model: each tx has multiple inputs (vin) and outputs (vout).
"""

import asyncio
import functools
import json
import time

import requests

from config.settings import settings
from shared.logger import setup_logger
from shared.models import WhaleMovement

logger = setup_logger("data_service")

_MEMPOOL_BASE = "https://mempool.space/api"

# Minimum seconds between API calls (public instance: ~10 req/min)
_MIN_CALL_INTERVAL = 0.5


class BtcWhaleClient:
    """Monitors BTC whale wallets for exchange deposits/withdrawals."""

    def __init__(self, price_provider=None):
        """
        Args:
            price_provider: Optional callable returning current BTC price in USD.
                            Used for USD conversion on whale movements.
        """
        self._whale_wallets = settings.BTC_WHALE_WALLETS
        self._exchange_addresses = settings.BTC_EXCHANGE_ADDRESSES
        self._price_provider = price_provider

        # Normalize exchange addresses to lowercase for comparison
        self._exchange_lookup: dict[str, str] = {
            addr.lower(): name
            for addr, name in self._exchange_addresses.items()
        }

        # Store detected movements (pruned to last 24h)
        self._movements: list[WhaleMovement] = []

        # Track last seen txid per wallet to avoid duplicates
        self._last_seen_tx: dict[str, str] = {}

        # Rate limiter
        self._last_call_time = 0.0

        self._running = False

        # Build address → label lookup
        self._wallet_labels: dict[str, str] = {
            addr.lower(): label
            for addr, label in self._whale_wallets.items()
        }

        if not self._whale_wallets:
            logger.warning("No BTC whale wallets configured")

    # ================================================================
    # Public interface
    # ================================================================

    def get_recent_movements(self, hours: int = 24) -> list[WhaleMovement]:
        """Get whale movements from the last N hours."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        return [m for m in self._movements if m.timestamp >= cutoff]

    def serialize_movements(self, hours: int = 24) -> list[dict]:
        """Serialize recent movements to list of dicts (for merging with ETH)."""
        movements = self.get_recent_movements(hours)
        records = []
        for m in movements:
            label = self._wallet_labels.get(m.wallet.lower(), m.wallet[:10] + "...")
            records.append({
                "timestamp": m.timestamp,
                "wallet": m.wallet,
                "label": label,
                "action": m.action,
                "amount": m.amount,
                "amount_usd": m.amount_usd,
                "market_price": m.market_price,
                "exchange": m.exchange,
                "significance": m.significance,
                "chain": m.chain,
            })
        return records

    # ================================================================
    # Polling loop
    # ================================================================

    async def start(self) -> None:
        """Start polling loop."""
        self._running = True

        if not self._whale_wallets:
            logger.info("BTC whale client: no wallets configured, polling disabled")
            return

        logger.info(f"BTC whale client started: monitoring {len(self._whale_wallets)} wallets "
                    f"every {settings.MEMPOOL_CHECK_INTERVAL}s")

        while self._running:
            await self._poll_all_wallets()
            await asyncio.sleep(settings.MEMPOOL_CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        logger.info("BTC whale client stopped")

    async def _poll_all_wallets(self) -> None:
        """Poll each wallet for recent transactions."""
        for wallet in self._whale_wallets.keys():
            if not self._running:
                break
            try:
                await self._check_wallet(wallet)
            except Exception as e:
                logger.error(f"Mempool error checking wallet {wallet[:10]}...: {e}")

    async def _check_wallet(self, wallet: str) -> None:
        """Fetch recent transactions for a wallet and detect exchange transfers."""
        await self._rate_limit()

        url = f"{_MEMPOOL_BASE}/address/{wallet}/txs"

        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                functools.partial(requests.get, url, timeout=15),
            )
            if resp.status_code == 429:
                logger.warning(f"Mempool rate limited: wallet={wallet[:10]}...")
                await asyncio.sleep(5)
                return
            if resp.status_code != 200:
                logger.warning(f"Mempool HTTP {resp.status_code}: wallet={wallet[:10]}...")
                return
            txs = resp.json()
        except requests.RequestException as e:
            logger.error(f"Mempool request failed: wallet={wallet[:10]}... error={e}")
            return
        except ValueError:
            logger.error(f"Mempool non-JSON response: wallet={wallet[:10]}...")
            return

        if not isinstance(txs, list) or not txs:
            return

        # Check for new transactions since last poll
        last_seen = self._last_seen_tx.get(wallet)

        # First poll for this wallet: seed baseline, don't generate events
        if last_seen is None:
            if txs:
                self._last_seen_tx[wallet] = txs[0].get("txid", "")
                label = self._wallet_labels.get(wallet.lower(), wallet[:10] + "...")
                logger.info(f"Baseline seeded for {label}")
            return

        new_txs = []
        for tx in txs[:25]:  # Only check last 25
            txid = tx.get("txid", "")
            if txid == last_seen:
                break
            new_txs.append(tx)

        if txs:
            self._last_seen_tx[wallet] = txs[0].get("txid", "")

        # Process new transactions for exchange transfers
        for tx in new_txs:
            self._process_transaction(wallet, tx)

    def _process_transaction(self, wallet: str, tx: dict) -> None:
        """Check if a transaction is an exchange deposit or withdrawal.

        BTC UTXO model:
        - vin[].prevout.scriptpubkey_address = sender addresses
        - vout[].scriptpubkey_address = recipient addresses
        - Values in satoshis (÷ 1e8 = BTC)
        """
        wallet_lower = wallet.lower()
        label = self._whale_wallets.get(wallet, "")

        # USD conversion
        market_price = 0.0
        if self._price_provider:
            try:
                market_price = self._price_provider()
            except Exception:
                pass

        # Get all input addresses
        input_addrs: dict[str, int] = {}  # addr → total satoshis
        for vin in tx.get("vin", []):
            prevout = vin.get("prevout", {})
            addr = prevout.get("scriptpubkey_address", "").lower()
            value = prevout.get("value", 0)
            if addr:
                input_addrs[addr] = input_addrs.get(addr, 0) + value

        # Get all output addresses
        output_addrs: dict[str, int] = {}  # addr → total satoshis
        for vout in tx.get("vout", []):
            addr = vout.get("scriptpubkey_address", "").lower()
            value = vout.get("value", 0)
            if addr:
                output_addrs[addr] = output_addrs.get(addr, 0) + value

        # Determine transaction timestamp (confirmed or first seen)
        status = tx.get("status", {})
        if status.get("confirmed") and status.get("block_time"):
            tx_timestamp = status["block_time"] * 1000  # Convert to ms
        else:
            # Unconfirmed — use current time
            tx_timestamp = int(time.time() * 1000)

        # Case 1: Wallet is sender → check if any output goes to exchange (deposit)
        found_exchange_output = False
        if wallet_lower in input_addrs:
            for out_addr, sats in output_addrs.items():
                if out_addr in self._exchange_lookup and out_addr != wallet_lower:
                    value_btc = sats / 1e8
                    if value_btc < settings.WHALE_MIN_BTC:
                        continue
                    found_exchange_output = True
                    exchange = self._exchange_lookup[out_addr]
                    significance = "high" if value_btc >= settings.WHALE_HIGH_BTC else "medium"
                    amount_usd = value_btc * market_price if market_price > 0 else 0.0
                    movement = WhaleMovement(
                        timestamp=tx_timestamp,
                        wallet=wallet,
                        action="exchange_deposit",
                        amount=value_btc,
                        exchange=exchange,
                        significance=significance,
                        chain="BTC",
                        wallet_label=label,
                        amount_usd=amount_usd,
                        market_price=market_price,
                    )
                    self._movements.append(movement)
                    usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
                    logger.info(f"BEARISH BTC whale deposit: {label or wallet[:10] + '...'} → {exchange} "
                                f"{value_btc:.4f} BTC{usd_str} [{significance}]")

            # No exchange output found → transfer out to non-exchange (neutral)
            if not found_exchange_output:
                non_self_sats = sum(
                    sats for addr, sats in output_addrs.items()
                    if addr != wallet_lower
                )
                value_btc = non_self_sats / 1e8
                if value_btc >= settings.WHALE_MIN_BTC:
                    # Pick first non-self output for the label
                    first_out = next(
                        (a for a in output_addrs if a != wallet_lower), ""
                    )
                    truncated = first_out[:6] + "..." + first_out[-4:] if first_out else "unknown"
                    significance = "high" if value_btc >= settings.WHALE_HIGH_BTC else "medium"
                    amount_usd = value_btc * market_price if market_price > 0 else 0.0
                    movement = WhaleMovement(
                        timestamp=tx_timestamp,
                        wallet=wallet,
                        action="transfer_out",
                        amount=value_btc,
                        exchange=truncated,
                        significance=significance,
                        chain="BTC",
                        wallet_label=label,
                        amount_usd=amount_usd,
                        market_price=market_price,
                    )
                    self._movements.append(movement)
                    usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
                    logger.info(f"NEUTRAL BTC whale transfer: {label or wallet[:10] + '...'} → {truncated} "
                                f"{value_btc:.4f} BTC{usd_str} [{significance}]")

        # Case 2: Exchange is sender → check if wallet is in outputs (withdrawal)
        found_exchange_input = False
        for in_addr in input_addrs:
            if in_addr in self._exchange_lookup:
                found_exchange_input = True
                if wallet_lower in output_addrs:
                    value_btc = output_addrs[wallet_lower] / 1e8
                    if value_btc < settings.WHALE_MIN_BTC:
                        continue
                    exchange = self._exchange_lookup[in_addr]
                    significance = "high" if value_btc >= settings.WHALE_HIGH_BTC else "medium"
                    amount_usd = value_btc * market_price if market_price > 0 else 0.0
                    movement = WhaleMovement(
                        timestamp=tx_timestamp,
                        wallet=wallet,
                        action="exchange_withdrawal",
                        amount=value_btc,
                        exchange=exchange,
                        significance=significance,
                        chain="BTC",
                        wallet_label=label,
                        amount_usd=amount_usd,
                        market_price=market_price,
                    )
                    self._movements.append(movement)
                    usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
                    logger.info(f"BULLISH BTC whale withdrawal: {label or wallet[:10] + '...'} ← {exchange} "
                                f"{value_btc:.4f} BTC{usd_str} [{significance}]")
                break  # Only count once per tx

        # Case 3: Wallet receives from non-exchange → transfer in (neutral)
        if (wallet_lower not in input_addrs
                and wallet_lower in output_addrs
                and not found_exchange_input):
            value_btc = output_addrs[wallet_lower] / 1e8
            if value_btc >= settings.WHALE_MIN_BTC:
                first_in = next(iter(input_addrs), "")
                truncated = first_in[:6] + "..." + first_in[-4:] if first_in else "unknown"
                significance = "high" if value_btc >= settings.WHALE_HIGH_BTC else "medium"
                amount_usd = value_btc * market_price if market_price > 0 else 0.0
                movement = WhaleMovement(
                    timestamp=tx_timestamp,
                    wallet=wallet,
                    action="transfer_in",
                    amount=value_btc,
                    exchange=truncated,
                    significance=significance,
                    chain="BTC",
                    wallet_label=label,
                    amount_usd=amount_usd,
                    market_price=market_price,
                )
                self._movements.append(movement)
                usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
                logger.info(f"NEUTRAL BTC whale transfer: {label or wallet[:10] + '...'} ← {truncated} "
                            f"{value_btc:.4f} BTC{usd_str} [{significance}]")

        # Prune old movements (keep last 24h)
        self._prune_old_movements()

    def _prune_old_movements(self) -> None:
        """Remove movements older than 24 hours."""
        cutoff = int((time.time() - 86400) * 1000)
        self._movements = [m for m in self._movements if m.timestamp >= cutoff]

    async def _rate_limit(self) -> None:
        """Enforce mempool.space rate limit."""
        now = time.time()
        elapsed = now - self._last_call_time
        if elapsed < _MIN_CALL_INTERVAL:
            await asyncio.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_call_time = time.time()
