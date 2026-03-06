"""
DataService facade — wires all data modules into a single interface.

This is the ONLY class that other services import from the data layer.
Strategy Service calls data_service.get_market_snapshot(pair),
not individual modules.

Startup sequence:
1. Connect Redis + PostgreSQL
2. Backfill 500 candles per pair/timeframe via OKX REST (ccxt)
3. Store backfilled candles in memory + PostgreSQL
4. Start WebSockets (OKX candles, OKX trades/CVD)
5. Start Etherscan polling
6. Start funding rate + OI polling loops
7. On every confirmed candle → store to Redis/PG → trigger pipeline callback
"""

import asyncio
import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import (
    Candle, FundingRate, OpenInterest, CVDSnapshot,
    LiquidationEvent, WhaleMovement, MarketSnapshot,
)

from data_service.exchange_client import ExchangeClient
from data_service.websocket_feeds import OKXWebSocketFeed
from data_service.cvd_calculator import CVDCalculator
from data_service.oi_liquidation_proxy import OILiquidationProxy
from data_service.etherscan_client import EtherscanClient
from data_service.btc_whale_client import BtcWhaleClient
from data_service.data_store import RedisStore, PostgresStore

logger = setup_logger("data_service")


class DataService:
    """Unified data layer for the trading bot.

    All other services interact with market data through this class.
    No direct imports of sub-modules outside of data_service/.
    """

    def __init__(self, on_candle_confirmed=None, notifier=None):
        """
        Args:
            on_candle_confirmed: Optional async callback triggered on each
                confirmed candle. Signature: async fn(candle: Candle).
                This is how main.py hooks the Strategy → AI → Risk → Execution pipeline.
            notifier: Optional TelegramNotifier for whale movement alerts.
        """
        self._pipeline_callback = on_candle_confirmed
        self._notifier = notifier

        # Sub-modules
        self._exchange = ExchangeClient()
        self._ws_feed = OKXWebSocketFeed(on_candle_confirmed=self._on_candle)
        self._cvd = CVDCalculator()
        self._oi_proxy = OILiquidationProxy()
        self._etherscan = EtherscanClient(price_provider=self._get_eth_price)
        self._btc_whale = BtcWhaleClient(price_provider=self._get_btc_price)
        self._redis = RedisStore()
        self._postgres = PostgresStore()

        # Async tasks for cleanup on shutdown
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ================================================================
    # Public API — called by Strategy Service and main.py
    # ================================================================

    def get_latest_candle(self, pair: str, timeframe: str) -> Optional[Candle]:
        """Get the most recent confirmed candle from memory."""
        return self._ws_feed.get_latest_candle(pair, timeframe)

    def get_candles(self, pair: str, timeframe: str,
                    count: int = 100) -> list[Candle]:
        """Get last N confirmed candles from memory (oldest first)."""
        return self._ws_feed.get_candles(pair, timeframe, count)

    def get_funding_rate(self, pair: str) -> Optional[FundingRate]:
        """Get latest cached funding rate from Redis."""
        return self._redis.get_funding_rate(pair)

    def get_open_interest(self, pair: str) -> Optional[OpenInterest]:
        """Get latest cached OI from Redis."""
        return self._redis.get_open_interest(pair)

    def get_cvd(self, pair: str) -> Optional[CVDSnapshot]:
        """Get latest CVD snapshot (recalculated every 5 seconds)."""
        return self._cvd.get_cvd(pair)

    def get_recent_liquidations(self, pair: str,
                                minutes: int = 60) -> list[LiquidationEvent]:
        """Get recent liquidation events from OI proxy."""
        return self._oi_proxy.get_recent_liquidations(pair, minutes)

    def get_liquidation_stats(self, pair: str,
                              minutes: int = 5) -> dict:
        """Get aggregated liquidation stats (total_usd, long_usd, short_usd, count)."""
        return self._oi_proxy.get_aggregated_stats(pair, minutes)

    def get_whale_movements(self, hours: int = 24) -> list[WhaleMovement]:
        """Get recent whale movements from Etherscan (ETH) and mempool.space (BTC)."""
        eth = self._etherscan.get_recent_movements(hours)
        btc = self._btc_whale.get_recent_movements(hours)
        combined = eth + btc
        combined.sort(key=lambda m: m.timestamp, reverse=True)
        return combined

    @property
    def postgres(self) -> PostgresStore:
        """Direct access to PostgreSQL store for trade persistence."""
        return self._postgres

    @property
    def redis(self) -> RedisStore:
        """Direct access to Redis store for state caching."""
        return self._redis

    def get_market_snapshot(self, pair: str) -> MarketSnapshot:
        """Assemble a complete MarketSnapshot for a pair.

        This is the main method the Strategy Service calls.
        It gathers data from all sources into a single object.
        """
        return MarketSnapshot(
            pair=pair,
            timestamp=int(time.time() * 1000),
            funding=self._redis.get_funding_rate(pair),
            oi=self._redis.get_open_interest(pair),
            cvd=self._cvd.get_cvd(pair),
            recent_liquidations=self._oi_proxy.get_recent_liquidations(pair, minutes=60),
            whale_movements=self.get_whale_movements(hours=24),
        )

    # ================================================================
    # Price providers for whale USD conversion
    # ================================================================

    def _get_eth_price(self) -> float:
        """Return latest ETH/USDT price from candle data."""
        candle = self._ws_feed.get_latest_candle("ETH/USDT", "5m")
        return candle.close if candle else 0.0

    def _get_btc_price(self) -> float:
        """Return latest BTC/USDT price from candle data."""
        candle = self._ws_feed.get_latest_candle("BTC/USDT", "5m")
        return candle.close if candle else 0.0

    # ================================================================
    # Health check
    # ================================================================

    def health(self) -> dict:
        """Return health status of all sub-modules."""
        return {
            "redis": self._redis.is_connected,
            "postgres": self._postgres.is_connected,
            "okx_ws": self._ws_feed.is_connected,
            "cvd_ws": self._cvd.is_connected,
            "oi_proxy": self._oi_proxy.is_connected,
            "running": self._running,
        }

    # ================================================================
    # Startup
    # ================================================================

    async def start(self) -> None:
        """Start all data modules in the correct order.

        1. Connect databases
        2. Backfill candles
        3. Launch all WebSockets and polling loops concurrently
        """
        self._running = True
        logger.info("DataService starting...")

        # Step 1: Connect databases
        self._connect_databases()

        # Step 2: Backfill candles from OKX REST (ccxt)
        await self._backfill_all()

        # Step 3: Initial fetch of funding rates and OI
        self._fetch_initial_indicators()

        # Step 4: Launch all async tasks
        logger.info("DataService: launching WebSockets and polling loops")

        self._tasks = [
            asyncio.create_task(self._ws_feed.start(), name="okx_ws"),
            asyncio.create_task(self._cvd.start(), name="okx_cvd_ws"),
            asyncio.create_task(self._etherscan_loop(), name="etherscan"),
            asyncio.create_task(self._btc_whale_loop(), name="btc_whale"),
            asyncio.create_task(self._funding_rate_loop(), name="funding_loop"),
            asyncio.create_task(self._oi_loop(), name="oi_loop"),
            asyncio.create_task(self._health_check_loop(), name="health_check"),
        ]

        logger.info(f"DataService started: {len(self._tasks)} background tasks running")

        # Wait for all tasks (they run forever until stop() is called)
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("DataService tasks cancelled")

    async def stop(self) -> None:
        """Gracefully stop all data modules."""
        self._running = False
        logger.info("DataService stopping...")

        # Stop sub-modules
        await self._ws_feed.stop()
        await self._cvd.stop()
        await self._etherscan.stop()
        await self._btc_whale.stop()

        # Cancel remaining tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        # Wait for cancellation to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Close database connections
        self._postgres.close()
        logger.info("DataService stopped")

    # ================================================================
    # Internal: Database connection
    # ================================================================

    def _connect_databases(self) -> None:
        """Connect to Redis and PostgreSQL."""
        redis_ok = self._redis.connect()
        pg_ok = self._postgres.connect()

        if not redis_ok:
            logger.warning("Redis unavailable — real-time caching disabled. "
                           "Bot will still work from memory.")
        if not pg_ok:
            logger.warning("PostgreSQL unavailable — historical storage disabled. "
                           "Bot will work from memory only (data lost on restart).")

    # ================================================================
    # Internal: Backfill
    # ================================================================

    async def _backfill_all(self) -> None:
        """Backfill 500 candles per pair/timeframe via OKX REST (ccxt).

        Runs in executor to avoid blocking the event loop (ccxt is synchronous).
        Stores results in memory (WebSocket feed) and PostgreSQL.
        """
        loop = asyncio.get_running_loop()
        all_timeframes = settings.HTF_TIMEFRAMES + settings.LTF_TIMEFRAMES

        for pair in settings.TRADING_PAIRS:
            for tf in all_timeframes:
                logger.info(f"Backfilling: pair={pair} tf={tf}")
                try:
                    candles = await loop.run_in_executor(
                        None, self._exchange.backfill_candles, pair, tf, 500
                    )

                    if not candles:
                        logger.warning(f"Backfill returned 0 candles: pair={pair} tf={tf}")
                        continue

                    # Store in memory
                    self._ws_feed.store_candles(candles)

                    # Store in PostgreSQL
                    inserted = await loop.run_in_executor(
                        None, self._postgres.store_candles, candles
                    )

                    # Cache latest in Redis
                    if candles:
                        self._redis.set_latest_candle(candles[-1])

                    logger.info(f"Backfill done: pair={pair} tf={tf} "
                                f"memory={len(candles)} pg_inserted={inserted}")

                except Exception as e:
                    logger.error(f"Backfill failed: pair={pair} tf={tf} error={e}")

    # ================================================================
    # Internal: Initial indicator fetch
    # ================================================================

    def _fetch_initial_indicators(self) -> None:
        """Fetch funding rates and OI for all pairs once at startup."""
        for pair in settings.TRADING_PAIRS:
            try:
                fr = self._exchange.fetch_funding_rate(pair)
                if fr:
                    self._redis.set_funding_rate(fr)
            except Exception as e:
                logger.error(f"Initial funding rate fetch failed: pair={pair} error={e}")

            try:
                oi = self._exchange.fetch_open_interest(pair)
                if oi:
                    self._redis.set_open_interest(oi)
                    self._oi_proxy.update(oi)
            except Exception as e:
                logger.error(f"Initial OI fetch failed: pair={pair} error={e}")

    # ================================================================
    # Internal: Candle confirmed callback
    # ================================================================

    async def _on_candle(self, candle: Candle) -> None:
        """Called by OKXWebSocketFeed on every confirmed candle.

        1. Store in Redis (latest cache)
        2. Store in PostgreSQL (historical)
        3. Trigger the pipeline callback (Strategy → AI → Risk → Execution)
        """
        # Cache in Redis
        self._redis.set_latest_candle(candle)

        # Store in PostgreSQL (run in executor since psycopg2 is sync)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._postgres.store_candles, [candle]
            )
        except Exception as e:
            logger.error(f"Failed to store candle in PostgreSQL: "
                         f"pair={candle.pair} tf={candle.timeframe} error={e}")

        # Track last processed timestamp
        self._redis.set_last_candle_ts(candle.pair, candle.timeframe, candle.timestamp)

        # Trigger downstream pipeline
        if self._pipeline_callback:
            try:
                await self._pipeline_callback(candle)
            except Exception as e:
                logger.error(f"Pipeline callback error: pair={candle.pair} "
                             f"tf={candle.timeframe} error={e}")

    # ================================================================
    # Internal: Polling loops
    # ================================================================

    async def _etherscan_loop(self) -> None:
        """Run Etherscan polling and publish whale movements to Redis after each cycle."""
        if not self._etherscan._api_key or not self._etherscan._whale_wallets:
            logger.info("Etherscan: no API key or wallets configured, polling disabled")
            return

        self._etherscan._running = True
        logger.info(f"Etherscan loop started: monitoring {len(self._etherscan._whale_wallets)} wallets "
                    f"every {settings.ETHERSCAN_CHECK_INTERVAL}s")

        while self._running:
            before_ids = {id(m) for m in self._etherscan._movements}
            await self._etherscan._poll_all_wallets()
            await self._notify_new_movements(self._etherscan._movements, before_ids)
            self._publish_whale_movements()
            await asyncio.sleep(settings.ETHERSCAN_CHECK_INTERVAL)

    async def _btc_whale_loop(self) -> None:
        """Run BTC whale polling via mempool.space and publish to Redis after each cycle."""
        if not self._btc_whale._whale_wallets:
            logger.info("BTC whale: no wallets configured, polling disabled")
            return

        self._btc_whale._running = True
        logger.info(f"BTC whale loop started: monitoring {len(self._btc_whale._whale_wallets)} wallets "
                    f"every {settings.MEMPOOL_CHECK_INTERVAL}s")

        while self._running:
            before_ids = {id(m) for m in self._btc_whale._movements}
            await self._btc_whale._poll_all_wallets()
            await self._notify_new_movements(self._btc_whale._movements, before_ids)
            self._publish_whale_movements()
            await asyncio.sleep(settings.MEMPOOL_CHECK_INTERVAL)

    async def _notify_new_movements(self, movements: list, before_ids: set) -> None:
        """Send Telegram alert for new whale movements with tiering.

        Tier 1 (always notify): Exchange deposits/withdrawals (actionable signals)
        Tier 2 (log only): Non-exchange transfers (informational, too noisy for Telegram)
        """
        if self._notifier is None:
            return
        new_movements = [m for m in movements if id(m) not in before_ids]
        for m in new_movements:
            # Only send Telegram for exchange-related movements
            if m.action not in ("exchange_deposit", "exchange_withdrawal"):
                continue
            try:
                await self._notifier.notify_whale_movement(m)
            except Exception as e:
                logger.error(f"Failed to send whale Telegram notification: {e}")

    def _publish_whale_movements(self) -> None:
        """Merge ETH + BTC whale movements and publish to Redis."""
        try:
            import json
            eth_records = json.loads(self._etherscan.serialize_movements(hours=24))
            btc_records = self._btc_whale.serialize_movements(hours=24)
            combined = eth_records + btc_records
            combined.sort(key=lambda r: r["timestamp"], reverse=True)
            self._redis.set_whale_movements(json.dumps(combined))
        except Exception as e:
            logger.error(f"Failed to publish whale movements to Redis: {e}")

    async def _funding_rate_loop(self) -> None:
        """Poll funding rates every FUNDING_RATE_INTERVAL seconds."""
        loop = asyncio.get_running_loop()

        while self._running:
            await asyncio.sleep(settings.FUNDING_RATE_INTERVAL)

            for pair in settings.TRADING_PAIRS:
                try:
                    fr = await loop.run_in_executor(
                        None, self._exchange.fetch_funding_rate, pair
                    )
                    if fr:
                        self._redis.set_funding_rate(fr)
                except Exception as e:
                    logger.error(f"Funding rate poll failed: pair={pair} error={e}")

    async def _oi_loop(self) -> None:
        """Poll open interest every OI_CHECK_INTERVAL seconds."""
        loop = asyncio.get_running_loop()

        while self._running:
            await asyncio.sleep(settings.OI_CHECK_INTERVAL)

            for pair in settings.TRADING_PAIRS:
                try:
                    oi = await loop.run_in_executor(
                        None, self._exchange.fetch_open_interest, pair
                    )
                    if oi:
                        self._redis.set_open_interest(oi)
                        self._oi_proxy.update(oi)
                except Exception as e:
                    logger.error(f"OI poll failed: pair={pair} error={e}")

    # ================================================================
    # Internal: Health check loop
    # ================================================================

    async def _health_check_loop(self) -> None:
        """Log health status every 30 seconds."""
        while self._running:
            await asyncio.sleep(30)
            status = self.health()
            connected = [k for k, v in status.items() if v is True]
            disconnected = [k for k, v in status.items() if v is False]

            if disconnected:
                logger.warning(f"Health check: OK={connected} DOWN={disconnected}")
            else:
                logger.debug(f"Health check: all systems OK")
