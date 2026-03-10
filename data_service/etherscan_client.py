"""
Etherscan client for monitoring whale ETH wallet movements.

Polls configured whale wallets every 5 minutes via Etherscan REST API.
Detects transfers to/from known exchange deposit addresses.

- Whale sends ETH to exchange → bearish signal (potential sell)
- Whale withdraws ETH from exchange → bullish signal (accumulation)

Rate limit: 5 calls/sec with free API key. Our usage: ~10 calls/5min = safe.
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

_ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"

# Rate limit: minimum seconds between API calls
# Free tier: 5 calls/sec, but V2 API is stricter under load.
# 30 wallets × 0.35s = ~10.5s per cycle — safe margin.
_MIN_CALL_INTERVAL = 0.35


class EtherscanClient:
    """Monitors whale wallets for exchange deposits/withdrawals.

    Other services call get_recent_movements() directly.
    """

    def __init__(self, price_provider=None):
        """
        Args:
            price_provider: Optional callable returning current ETH price in USD.
                            Used for USD conversion on whale movements.
        """
        self._api_key = settings.ETHERSCAN_API_KEY
        self._whale_wallets = settings.WHALE_WALLETS
        self._exchange_addresses = settings.EXCHANGE_ADDRESSES
        self._price_provider = price_provider

        # Normalize exchange addresses to lowercase for comparison
        self._exchange_lookup: dict[str, str] = {
            addr.lower(): name
            for addr, name in self._exchange_addresses.items()
        }

        # Store detected movements (pruned to last 24h)
        self._movements: list[WhaleMovement] = []

        # Track last seen tx hash per wallet to avoid duplicates
        self._last_seen_tx: dict[str, str] = {}

        # Rate limiter
        self._last_call_time = 0.0

        self._running = False

        # Build address → label lookup
        self._wallet_labels: dict[str, str] = {
            addr.lower(): label
            for addr, label in self._whale_wallets.items()
        }

        if not self._api_key:
            logger.warning("Etherscan API key not set — whale monitoring disabled")
        if not self._whale_wallets:
            logger.warning("No whale wallets configured — add addresses to settings.WHALE_WALLETS")

    # ================================================================
    # Public interface
    # ================================================================

    def get_recent_movements(self, hours: int = 24) -> list[WhaleMovement]:
        """Get whale movements from the last N hours."""
        cutoff = int((time.time() - hours * 3600) * 1000)
        return [m for m in self._movements if m.timestamp >= cutoff]

    def serialize_movements(self, hours: int = 24) -> list[dict]:
        """Serialize recent movements to list of dicts including wallet labels."""
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
        """Start polling loop. Checks all wallets every ETHERSCAN_CHECK_INTERVAL seconds."""
        self._running = True

        if not self._api_key or not self._whale_wallets:
            logger.info("Etherscan client: no API key or wallets configured, polling disabled")
            return

        logger.info(f"Etherscan client started: monitoring {len(self._whale_wallets)} wallets "
                    f"every {settings.ETHERSCAN_CHECK_INTERVAL}s")

        while self._running:
            await self._poll_all_wallets()
            await asyncio.sleep(settings.ETHERSCAN_CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        logger.info("Etherscan client stopped")

    async def _poll_all_wallets(self) -> None:
        """Poll each wallet for recent transactions."""
        for wallet in self._whale_wallets.keys():
            if not self._running:
                break
            try:
                await self._check_wallet(wallet)
            except Exception as e:
                logger.error(f"Etherscan error checking wallet {wallet[:10]}...: {e}")

    async def _check_wallet(self, wallet: str) -> None:
        """Fetch recent transactions for a wallet and detect exchange transfers."""
        await self._rate_limit()

        params = {
            "chainid": 1,  # Ethereum mainnet
            "module": "account",
            "action": "txlist",
            "address": wallet,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": 20,  # Last 20 transactions
            "sort": "desc",
            "apikey": self._api_key,
        }

        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                functools.partial(requests.get, _ETHERSCAN_BASE, params=params, timeout=15),
            )
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Etherscan request failed: wallet={wallet[:10]}... error={e}")
            return
        except ValueError:
            logger.error(f"Etherscan non-JSON response: wallet={wallet[:10]}... "
                         f"status={resp.status_code}")
            return

        # Etherscan returns status "0" with message "NOTOK" on errors/rate limits
        if data.get("status") == "0":
            message = data.get("message", "")
            result = data.get("result", "")
            if "NOTOK" in str(message) or "rate limit" in str(result).lower():
                logger.warning(f"Etherscan rate limited: wallet={wallet[:10]}... "
                               f"msg={message} result={result}")
                await asyncio.sleep(5)
                return
            # "No transactions found" is status "0" but not an error
            if "No transactions found" in str(data.get("result", "")):
                logger.debug(f"Etherscan: no transactions for {wallet[:10]}...")
                return
            logger.warning(f"Etherscan status 0: wallet={wallet[:10]}... "
                           f"msg={message} result={result}")
            return

        txs = data.get("result", [])
        if not isinstance(txs, list):
            logger.warning(f"Etherscan unexpected result type: wallet={wallet[:10]}... "
                           f"type={type(txs)}")
            return

        if not txs:
            logger.debug(f"Etherscan: no recent transactions for {wallet[:10]}...")
            return

        # Check for new transactions since last poll
        last_seen = self._last_seen_tx.get(wallet)
        new_txs = []
        for tx in txs:
            tx_hash = tx.get("hash", "")
            if tx_hash == last_seen:
                break
            new_txs.append(tx)

        if txs:
            self._last_seen_tx[wallet] = txs[0].get("hash", "")

        # Process new transactions for exchange transfers
        for tx in new_txs:
            self._process_transaction(wallet, tx)

    def _process_transaction(self, wallet: str, tx: dict) -> None:
        """Check if a transaction is an exchange deposit or withdrawal."""
        value_wei = int(tx.get("value", "0"))
        value_eth = value_wei / 1e18

        # Skip small transfers
        if value_eth < settings.WHALE_MIN_ETH:
            return

        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        wallet_lower = wallet.lower()
        tx_timestamp = int(tx.get("timeStamp", "0")) * 1000  # Convert to ms
        label = self._whale_wallets.get(wallet, "")

        # Determine significance
        if value_eth >= settings.WHALE_HIGH_ETH:
            significance = "high"
        else:
            significance = "medium"

        # USD conversion
        market_price = 0.0
        if self._price_provider:
            try:
                market_price = self._price_provider()
            except Exception:
                pass
        amount_usd = value_eth * market_price if market_price > 0 else 0.0

        # Wallet sends TO an exchange → deposit (bearish)
        if from_addr == wallet_lower and to_addr in self._exchange_lookup:
            exchange = self._exchange_lookup[to_addr]
            movement = WhaleMovement(
                timestamp=tx_timestamp,
                wallet=wallet,
                action="exchange_deposit",
                amount=value_eth,
                exchange=exchange,
                significance=significance,
                chain="ETH",
                wallet_label=label,
                amount_usd=amount_usd,
                market_price=market_price,
            )
            self._movements.append(movement)
            usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
            logger.info(f"BEARISH Whale deposit: {label or wallet[:10] + '...'} → {exchange} "
                        f"{value_eth:.2f} ETH{usd_str} [{significance}]")

        # Wallet receives FROM an exchange → withdrawal (bullish)
        elif to_addr == wallet_lower and from_addr in self._exchange_lookup:
            exchange = self._exchange_lookup[from_addr]
            movement = WhaleMovement(
                timestamp=tx_timestamp,
                wallet=wallet,
                action="exchange_withdrawal",
                amount=value_eth,
                exchange=exchange,
                significance=significance,
                chain="ETH",
                wallet_label=label,
                amount_usd=amount_usd,
                market_price=market_price,
            )
            self._movements.append(movement)
            usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
            logger.info(f"BULLISH Whale withdrawal: {label or wallet[:10] + '...'} ← {exchange} "
                        f"{value_eth:.2f} ETH{usd_str} [{significance}]")

        # Wallet sends to non-exchange address → transfer out (neutral)
        elif from_addr == wallet_lower:
            truncated = to_addr[:6] + "..." + to_addr[-4:]
            movement = WhaleMovement(
                timestamp=tx_timestamp,
                wallet=wallet,
                action="transfer_out",
                amount=value_eth,
                exchange=truncated,
                significance=significance,
                chain="ETH",
                wallet_label=label,
                amount_usd=amount_usd,
                market_price=market_price,
            )
            self._movements.append(movement)
            usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
            logger.info(f"NEUTRAL Whale transfer: {label or wallet[:10] + '...'} → {truncated} "
                        f"{value_eth:.2f} ETH{usd_str} [{significance}]")

        # Wallet receives from non-exchange address → transfer in (neutral)
        elif to_addr == wallet_lower:
            truncated = from_addr[:6] + "..." + from_addr[-4:]
            movement = WhaleMovement(
                timestamp=tx_timestamp,
                wallet=wallet,
                action="transfer_in",
                amount=value_eth,
                exchange=truncated,
                significance=significance,
                chain="ETH",
                wallet_label=label,
                amount_usd=amount_usd,
                market_price=market_price,
            )
            self._movements.append(movement)
            usd_str = f" (~${amount_usd:,.0f})" if amount_usd > 0 else ""
            logger.info(f"NEUTRAL Whale transfer: {label or wallet[:10] + '...'} ← {truncated} "
                        f"{value_eth:.2f} ETH{usd_str} [{significance}]")

        # Prune old movements (keep last 24h)
        self._prune_old_movements()

    def _prune_old_movements(self) -> None:
        """Remove movements older than 24 hours."""
        cutoff = int((time.time() - 86400) * 1000)
        self._movements = [m for m in self._movements if m.timestamp >= cutoff]

    async def _rate_limit(self) -> None:
        """Enforce Etherscan rate limit of 5 calls/sec."""
        now = time.time()
        elapsed = now - self._last_call_time
        if elapsed < _MIN_CALL_INTERVAL:
            await asyncio.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_call_time = time.time()
