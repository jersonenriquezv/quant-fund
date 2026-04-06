"""
OKX REST client via ccxt — backfill, funding rates, open interest.

Handles:
- Historical candle backfill (500 per pair/timeframe on startup)
- Funding rate polling (every 8 hours — OKX standard schedule)
- Open interest polling (every 5 minutes)

Does NOT handle WebSocket streaming — see websocket_feeds.py.

Rate limits (OKX): 20 requests/2s for market data, 60 requests/2s for trading.
ccxt handles basic throttling; we add validation on top.
"""

import time
from typing import Optional

import ccxt

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, FundingRate, OpenInterest
from data_service.data_integrity import CONTRACT_SIZES

logger = setup_logger("data_service")

# OKX uses instId format "BTC-USDT-SWAP" for perpetuals.
# ccxt accepts "BTC/USDT:USDT" and translates internally.
_PAIR_TO_SYMBOL = {
    "BTC/USDT": "BTC-USDT-SWAP",
    "ETH/USDT": "ETH-USDT-SWAP",
    "SOL/USDT": "SOL-USDT-SWAP",
    "DOGE/USDT": "DOGE-USDT-SWAP",
    "XRP/USDT": "XRP-USDT-SWAP",
    "LINK/USDT": "LINK-USDT-SWAP",
    "AVAX/USDT": "AVAX-USDT-SWAP",
}

# OKX max candles per request
_MAX_CANDLES_PER_REQUEST = 100

# Contract sizes imported from data_integrity.py (single source of truth)
# _CONTRACT_SIZES alias kept for backward compatibility within this module
_CONTRACT_SIZES = CONTRACT_SIZES

# ccxt expects lowercase timeframes: "5m", "15m", "1h", "4h"
# OKX API uses "1H"/"4H" but ccxt handles the conversion internally.
# Do NOT map to uppercase — that bypasses ccxt and breaks sandbox mode.


class ExchangeClient:
    """OKX REST client for market data. All methods return shared/models.py types."""

    def __init__(self):
        config = {
            "apiKey": settings.OKX_API_KEY,
            "secret": settings.OKX_SECRET,
            "password": settings.OKX_PASSPHRASE,
            "enableRateLimit": True,
        }

        self._exchange = ccxt.okx(config)

        if settings.OKX_SANDBOX:
            self._exchange.set_sandbox_mode(True)
            logger.info("OKX client initialized in DEMO/SANDBOX mode")
        else:
            logger.info("OKX client initialized in LIVE mode")

        # Separate production client for public market data (candles, funding, OI).
        # Sandbox prices differ from real market — market data must always come
        # from production so the dashboard shows real prices.
        if settings.OKX_SANDBOX:
            self._market_exchange = ccxt.okx({"enableRateLimit": True})
            logger.info("OKX market data client: using PRODUCTION for real prices")
        else:
            self._market_exchange = self._exchange

    def fetch_usdt_balance(self) -> float | None:
        """Fetch USDT available balance from exchange. Returns None on failure."""
        try:
            balance = self._exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            free = usdt.get("free", 0.0)
            return float(free) if free else 0.0
        except Exception as e:
            logger.warning(f"Failed to fetch USDT balance: {e}")
            return None

    def _ccxt_symbol(self, pair: str) -> str:
        """Convert our pair format to ccxt symbol for OKX.

        "BTC/USDT" → "BTC/USDT:USDT" (ccxt swap format for OKX).
        """
        return f"{pair}:USDT"

    def _validate_candle(self, pair: str, timeframe: str, ts: int,
                         o: float, h: float, l: float, c: float,
                         vol: float) -> bool:
        """Validate candle data per data-engineer rules.
        Returns True if valid, False if should be discarded.
        """
        now_ms = int(time.time() * 1000)

        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            logger.error(f"Candle with price <= 0 discarded: pair={pair} tf={timeframe} "
                         f"ts={ts} OHLC=[{o},{h},{l},{c}]")
            return False

        if vol == 0:
            logger.warning(f"Candle with zero volume discarded: pair={pair} tf={timeframe} ts={ts}")
            return False

        if ts > now_ms + 60_000:
            logger.warning(f"Candle with future timestamp discarded: pair={pair} tf={timeframe} "
                           f"ts={ts} (now={now_ms}, diff={ts - now_ms}ms)")
            return False

        return True

    # ================================================================
    # Backfill — fetch historical candles on startup
    # ================================================================

    def backfill_candles(self, pair: str, timeframe: str,
                         count: int = 500) -> list[Candle]:
        """Fetch last `count` closed candles via OKX REST (through ccxt).

        All returned candles are confirmed (historical = already closed).
        OKX returns max 100 candles per request, so we paginate.

        Args:
            pair: "BTC/USDT" or "ETH/USDT"
            timeframe: "5m", "15m", "1h", "4h"
            count: number of candles to fetch (default 500)

        Returns:
            List of Candle sorted by timestamp ascending (oldest first).
        """
        tf = timeframe  # Pass as-is; ccxt maps to OKX format internally
        symbol = self._ccxt_symbol(pair)
        candles: list[Candle] = []
        since = None
        requests_made = 0

        logger.info(f"Backfill starting: pair={pair} tf={timeframe} target={count} candles")

        all_ohlcv = []
        while len(all_ohlcv) < count:
            try:
                limit = min(_MAX_CANDLES_PER_REQUEST, count - len(all_ohlcv))
                batch = self._market_exchange.fetch_ohlcv(
                    symbol, tf, since=since, limit=limit
                )
                requests_made += 1

                if not batch:
                    logger.warning(f"Backfill: empty response at request #{requests_made} "
                                   f"pair={pair} tf={timeframe}")
                    break

                all_ohlcv = batch + all_ohlcv  # prepend older candles

                # If we got fewer than requested, we've reached the beginning
                if len(batch) < limit:
                    logger.info(f"Backfill: reached end of available data at request #{requests_made}")
                    break

                # For pagination: fetch candles before the oldest one we have
                oldest_ts = batch[0][0]
                tf_ms = self._timeframe_to_ms(timeframe)
                since = oldest_ts - (limit * tf_ms)

            except ccxt.RateLimitExceeded as e:
                logger.warning(f"Backfill rate limited: pair={pair} tf={timeframe}. "
                               f"Waiting 2s. Error: {e}")
                time.sleep(2)
                continue
            except ccxt.NetworkError as e:
                logger.error(f"Backfill network error: pair={pair} tf={timeframe}. Error: {e}")
                time.sleep(5)
                continue
            except ccxt.ExchangeError as e:
                logger.error(f"Backfill exchange error: pair={pair} tf={timeframe}. Error: {e}")
                break

        # Convert to Candle dataclasses with validation
        for row in all_ohlcv:
            ts, o, h, l, c, vol = row[0], row[1], row[2], row[3], row[4], row[5]

            if not self._validate_candle(pair, timeframe, ts, o, h, l, c, vol):
                continue

            candles.append(Candle(
                timestamp=ts,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=vol,
                volume_quote=vol * c,  # Approximate USDT volume
                pair=pair,
                timeframe=timeframe,
                confirmed=True,  # Historical candles are always confirmed
            ))

        logger.info(f"Backfill complete: pair={pair} tf={timeframe} "
                    f"candles={len(candles)} requests={requests_made}")
        return candles

    # ================================================================
    # Funding rate — poll every 8 hours (OKX standard)
    # ================================================================

    def fetch_funding_rate(self, pair: str) -> Optional[FundingRate]:
        """Fetch current funding rate from OKX.

        OKX charges funding every 8 hours (standard CEX schedule).
        """
        try:
            symbol = self._ccxt_symbol(pair)
            data = self._market_exchange.fetch_funding_rate(symbol)

            rate = data.get("fundingRate")
            next_rate = data.get("nextFundingRate")
            funding_ts = data.get("fundingTimestamp") or data.get("timestamp")
            next_funding_time = data.get("nextFundingTimestamp", 0)

            if rate is None:
                logger.error(f"Funding rate response missing 'fundingRate': "
                             f"pair={pair} data={data}")
                return None

            fr = FundingRate(
                timestamp=int(funding_ts or time.time() * 1000),
                pair=pair,
                rate=float(rate),
                next_rate=float(next_rate) if next_rate is not None else 0.0,
                next_funding_time=int(next_funding_time or 0),
                fetched_at=int(time.time() * 1000),
            )

            logger.info(f"Funding rate fetched: pair={pair} rate={fr.rate:.6f} "
                        f"next_rate={fr.next_rate:.6f}")
            return fr

        except ccxt.RateLimitExceeded as e:
            logger.warning(f"Funding rate rate limited: pair={pair}. Error: {e}")
            return None
        except ccxt.NetworkError as e:
            logger.error(f"Funding rate network error: pair={pair}. Error: {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"Funding rate exchange error: pair={pair}. Error: {e}")
            return None

    # ================================================================
    # Open Interest — poll every 5 minutes
    # ================================================================

    def fetch_open_interest(self, pair: str) -> Optional[OpenInterest]:
        """Fetch current open interest from OKX.

        OI doesn't change drastically second-to-second.
        Polling every 5 minutes is sufficient.
        """
        try:
            symbol = self._ccxt_symbol(pair)
            data = self._market_exchange.fetch_open_interest(symbol)

            oi_value = data.get("openInterestAmount") or data.get("openInterest")
            if oi_value is None:
                logger.error(f"OI response missing value: pair={pair} data={data}")
                return None

            oi_base = float(oi_value)
            ts = data.get("timestamp") or int(time.time() * 1000)

            # Get current price for USD conversion
            ticker = self._market_exchange.fetch_ticker(symbol)
            price = ticker.get("last", 0)

            # Derive contract count from base amount and contract size
            contract_size = _CONTRACT_SIZES.get(pair, 1.0)
            oi_contracts = oi_base / contract_size if contract_size else oi_base

            oi = OpenInterest(
                timestamp=int(ts),
                pair=pair,
                oi_contracts=oi_contracts,
                oi_base=oi_base,
                oi_usd=oi_base * float(price) if price else 0.0,
            )

            logger.info(f"OI fetched: pair={pair} oi_base={oi.oi_base:.4f} "
                        f"oi_usd=${oi.oi_usd:,.0f}")
            return oi

        except ccxt.RateLimitExceeded as e:
            logger.warning(f"OI rate limited: pair={pair}. Error: {e}")
            return None
        except ccxt.NetworkError as e:
            logger.error(f"OI network error: pair={pair}. Error: {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"OI exchange error: pair={pair}. Error: {e}")
            return None

    def fetch_orderbook_snapshot(self, pair: str, depth: int = 5) -> dict | None:
        """Fetch L2 orderbook snapshot for spread and depth estimation.

        Returns dict with best_bid, best_ask, spread, depth_bid_usd, depth_ask_usd
        (cumulative USD within ±0.1% of mid price). Returns None on failure.
        """
        try:
            symbol = self._ccxt_symbol(pair)
            book = self._market_exchange.fetch_order_book(symbol, limit=depth)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return None

            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2
            spread = (best_ask - best_bid) / mid if mid > 0 else 0

            # Cumulative depth within ±0.1% of mid
            threshold = mid * 0.001
            depth_bid_usd = sum(
                level[0] * level[1] for level in bids if level[0] >= mid - threshold
            )
            depth_ask_usd = sum(
                level[0] * level[1] for level in asks if level[0] <= mid + threshold
            )

            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "depth_bid_usd": depth_bid_usd,
                "depth_ask_usd": depth_ask_usd,
            }
        except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeError) as e:
            logger.debug(f"Orderbook fetch failed: pair={pair} {e}")
            return None

    def fetch_orderbook_depth(
        self, pair: str, levels: int = 20,
    ) -> dict | None:
        """Fetch L2 orderbook with raw level data for depth analysis.

        Returns dict with bids/asks as [(price, size_usd), ...] and metadata.
        Used by strategy layer to confirm OB zones with real liquidity.
        """
        try:
            symbol = self._ccxt_symbol(pair)
            book = self._market_exchange.fetch_order_book(symbol, limit=levels)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if not bids or not asks:
                return None

            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2
            timestamp_ms = int(time.time() * 1000)

            # Raw levels as (price, size_usd) tuples
            bid_levels = [(b[0], b[0] * b[1]) for b in bids]
            ask_levels = [(a[0], a[0] * a[1]) for a in asks]

            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "bid_levels": bid_levels,
                "ask_levels": ask_levels,
                "timestamp_ms": timestamp_ms,
            }
        except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeError) as e:
            logger.debug(f"Orderbook depth fetch failed: pair={pair} {e}")
            return None

    # ================================================================
    # Historical funding rates — for backtest backfill
    # ================================================================

    def fetch_funding_rate_history(self, pair: str,
                                   since_ms: int | None = None,
                                   limit: int = 100) -> list[dict]:
        """Fetch historical funding rates from OKX via ccxt.

        Returns list of dicts: {timestamp, rate, next_rate}.
        OKX provides ~3 months of history. Max 100 per request.
        """
        try:
            symbol = self._ccxt_symbol(pair)
            records = self._market_exchange.fetch_funding_rate_history(
                symbol, since=since_ms, limit=limit
            )
            results = []
            for r in records:
                results.append({
                    "timestamp": r.get("timestamp", 0),
                    "rate": r.get("fundingRate", 0.0),
                    "next_rate": r.get("nextFundingRate", 0.0) or 0.0,
                })
            return results
        except ccxt.RateLimitExceeded as e:
            logger.warning(f"Funding history rate limited: pair={pair}. Error: {e}")
            return []
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"Funding history fetch failed: pair={pair}. Error: {e}")
            return []

    # ================================================================
    # Historical open interest — for backtest backfill
    # ================================================================

    def fetch_open_interest_history(self, pair: str,
                                    since_ms: int | None = None,
                                    limit: int = 100,
                                    timeframe: str = "1h") -> list[dict]:
        """Fetch historical OI from OKX via ccxt.

        OKX limits: 1h goes back ~30 days, 1D goes back ~99 days.
        OKX returns openInterestValue (USD) only — base/contracts are 0.

        Returns list of dicts: {timestamp, oi_contracts, oi_base, oi_usd}.
        """
        try:
            symbol = self._ccxt_symbol(pair)
            records = self._market_exchange.fetch_open_interest_history(
                symbol, timeframe=timeframe, since=since_ms, limit=limit
            )
            results = []
            for r in records:
                ts = r.get("timestamp", 0)
                oi_usd = float(r.get("openInterestValue", 0) or 0)
                if oi_usd <= 0:
                    continue
                results.append({
                    "timestamp": ts,
                    "oi_contracts": 0.0,
                    "oi_base": 0.0,
                    "oi_usd": oi_usd,
                })
            return results
        except ccxt.RateLimitExceeded as e:
            logger.warning(f"OI history rate limited: pair={pair}. Error: {e}")
            return []
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"OI history fetch failed: pair={pair}. Error: {e}")
            return []

    # ================================================================
    # Helpers
    # ================================================================

    @staticmethod
    def _timeframe_to_ms(timeframe: str) -> int:
        """Convert timeframe string to milliseconds."""
        multipliers = {
            "1m": 1 * 60 * 1000,
            "5m": 5 * 60 * 1000,
            "15m": 15 * 60 * 1000,
            "1h": 60 * 60 * 1000,
            "4h": 4 * 60 * 60 * 1000,
            "1d": 24 * 60 * 60 * 1000,
        }
        if timeframe not in multipliers:
            raise ValueError(f"Unknown timeframe: {timeframe}")
        return multipliers[timeframe]
