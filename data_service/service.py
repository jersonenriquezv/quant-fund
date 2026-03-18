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
    OIFlushEvent, WhaleMovement, MarketSnapshot, NewsSentiment,
    SourceFreshness, SnapshotHealth,
)

from data_service.exchange_client import ExchangeClient
from data_service.websocket_feeds import OKXWebSocketFeed
from data_service.cvd_calculator import CVDCalculator
from data_service.oi_flush_detector import OIFlushDetector
from data_service.etherscan_client import EtherscanClient
from data_service.btc_whale_client import BtcWhaleClient
from data_service.data_store import RedisStore, PostgresStore
from data_service.news_client import NewsClient
from data_service.data_integrity import (
    DataServiceState, CVDState, CircuitBreaker,
    validate_candle_continuity, TIMEFRAME_MS,
)

logger = setup_logger("data_service")


class DataService:
    """Unified data layer for the trading bot.

    All other services interact with market data through this class.
    No direct imports of sub-modules outside of data_service/.
    """

    def __init__(self, on_candle_confirmed=None, alert_manager=None):
        """
        Args:
            on_candle_confirmed: Optional async callback triggered on each
                confirmed candle. Signature: async fn(candle: Candle).
                This is how main.py hooks the Strategy → AI → Risk → Execution pipeline.
            alert_manager: Optional AlertManager for whale movement and health alerts.
        """
        self._pipeline_callback = on_candle_confirmed
        self._alert_manager = alert_manager

        # Sub-modules
        self._exchange = ExchangeClient()
        self._ws_feed = OKXWebSocketFeed(
            on_candle_confirmed=self._on_candle,
            metrics_callback=self._emit_metric,
        )
        self._cvd = CVDCalculator()
        self._oi_proxy = OIFlushDetector()
        self._etherscan = EtherscanClient(price_provider=self._get_eth_price)
        self._btc_whale = BtcWhaleClient(price_provider=self._get_btc_price)
        self._redis = RedisStore()
        self._postgres = PostgresStore()
        self._news = NewsClient(redis_store=self._redis)

        # Latest sentiment data (refreshed by polling loop)
        self._latest_sentiment: Optional[NewsSentiment] = None

        # Async tasks for cleanup on shutdown
        self._tasks: list[asyncio.Task] = []
        self._running = False

        # Health check state — track which components were down last check
        self._last_health_down: set[str] = set()
        # Metrics cleanup counter (runs every ~100 health checks = ~50 min)
        self._health_check_count: int = 0

        # Data integrity: global state, circuit breaker, backfill guard
        self._state: DataServiceState = DataServiceState.RECOVERING
        self._circuit_breaker = CircuitBreaker(
            max_events=settings.CIRCUIT_BREAKER_MAX_RECONNECTS,
            window_seconds=settings.CIRCUIT_BREAKER_WINDOW_SECONDS,
            stable_seconds=settings.CIRCUIT_BREAKER_STABLE_SECONDS,
        )
        self._backfill_in_progress: bool = False

        # Register reconnect callback for gap backfill
        self._ws_feed._on_reconnect_cb = self._on_ws_reconnect

    # ================================================================
    # Public API — called by Strategy Service and main.py
    # ================================================================

    @property
    def state(self) -> DataServiceState:
        """Current global state of the DataService."""
        return self._state

    def get_cvd_state(self, pair: str) -> CVDState:
        """Get CVD validity state for a pair."""
        return self._cvd.get_cvd_state(pair)

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

    def get_recent_oi_flushes(self, pair: str,
                              minutes: int = 60) -> list[OIFlushEvent]:
        """Get recent OI flush events from OI flush detector."""
        return self._oi_proxy.get_recent_oi_flushes(pair, minutes)

    def get_oi_flush_stats(self, pair: str,
                           minutes: int = 5) -> dict:
        """Get aggregated OI flush stats (total_usd, long_usd, short_usd, count)."""
        return self._oi_proxy.get_aggregated_stats(pair, minutes)

    def get_whale_movements(self, hours: int = 24) -> list[WhaleMovement]:
        """Get recent whale movements from Etherscan (ETH) and mempool.space (BTC)."""
        eth = self._etherscan.get_recent_movements(hours)
        btc = self._btc_whale.get_recent_movements(hours)
        combined = eth + btc
        combined.sort(key=lambda m: m.timestamp, reverse=True)
        return combined

    def fetch_usdt_balance(self) -> float | None:
        """Fetch USDT balance from exchange. Returns None on failure."""
        return self._exchange.fetch_usdt_balance()

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
        now_ms = int(time.time() * 1000)
        funding = self._redis.get_funding_rate(pair)
        oi = self._redis.get_open_interest(pair)
        cvd = self._cvd.get_cvd(pair)
        oi_flushes = self._oi_proxy.get_recent_oi_flushes(pair, minutes=60)
        whales = self.get_whale_movements(hours=24)
        news = self._latest_sentiment

        health = self._compute_health(now_ms, funding, oi, cvd, whales, news)

        if not health.critical_sources_healthy:
            logger.warning(
                f"Snapshot degraded: pair={pair} "
                f"stale={list(health.stale_sources)} missing={list(health.missing_sources)}"
            )
        elif health.completeness_pct < 0.5:
            logger.warning(
                f"Snapshot heavily degraded: pair={pair} "
                f"completeness={health.completeness_pct:.0%}"
            )

        return MarketSnapshot(
            pair=pair,
            timestamp=now_ms,
            funding=funding,
            oi=oi,
            cvd=cvd,
            recent_oi_flushes=oi_flushes,
            whale_movements=whales,
            news_sentiment=news,
            health=health,
        )

    # ================================================================
    # Snapshot health computation
    # ================================================================

    def _compute_health(
        self, now_ms: int,
        funding: FundingRate | None,
        oi: OpenInterest | None,
        cvd: CVDSnapshot | None,
        whales: list[WhaleMovement],
        news: NewsSentiment | None,
    ) -> SnapshotHealth:
        """Compute freshness and completeness of a snapshot's data sources."""
        sources = []

        def _add(name: str, priority: str, ts: int | None, stale_ms: int):
            if ts is None:
                sources.append(SourceFreshness(name=name, priority=priority, age_ms=None, is_stale=True))
            else:
                age = now_ms - ts
                sources.append(SourceFreshness(name=name, priority=priority, age_ms=age, is_stale=age > stale_ms))

        # Critical sources
        # Funding: use fetched_at if available (actual fetch time), fall back to timestamp
        funding_ts = None
        if funding:
            funding_ts = funding.fetched_at if funding.fetched_at > 0 else funding.timestamp
        _add("funding", "critical", funding_ts, settings.FUNDING_STALE_MS)
        _add("oi", "critical", oi.timestamp if oi else None, settings.OI_STALE_MS)
        _add("cvd", "critical", cvd.timestamp if cvd else None, settings.CVD_STALE_MS)

        # Decorative sources
        latest_whale_ts = max((w.timestamp for w in whales), default=None) if whales else None
        _add("whales", "decorative", latest_whale_ts, settings.WHALE_STALE_MS)
        _add("news", "decorative", news.fetched_at if news else None, settings.NEWS_STALE_MS)

        available = sum(1 for s in sources if s.age_ms is not None)
        completeness = available / len(sources) if sources else 0.0
        stale = tuple(s.name for s in sources if s.is_stale and s.age_ms is not None)
        missing = tuple(s.name for s in sources if s.age_ms is None)
        critical_ok = all(not s.is_stale for s in sources if s.priority == "critical")

        # Redis health — if Redis is down, critical sources are degraded
        redis_ok = self._redis.is_connected
        if not redis_ok:
            critical_ok = False

        return SnapshotHealth(
            sources=tuple(sources),
            completeness_pct=round(completeness, 2),
            critical_sources_healthy=critical_ok,
            stale_sources=stale,
            missing_sources=missing,
            redis_healthy=redis_ok,
            service_state=self._state.value,
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
        4. Warmup loop transitions state to RUNNING when ready
        """
        self._running = True
        self._state = DataServiceState.RECOVERING
        logger.info(f"DataService starting... state={self._state.name}")

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
            asyncio.create_task(self._news_sentiment_loop(), name="news_sentiment"),
            asyncio.create_task(self._health_check_loop(), name="health_check"),
            asyncio.create_task(self._warmup_check_loop(), name="warmup_check"),
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
        await self._news.close()

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
        if self._backfill_in_progress:
            logger.warning("Backfill already in progress — skipping duplicate request")
            return

        self._backfill_in_progress = True
        loop = asyncio.get_running_loop()
        all_timeframes = settings.HTF_TIMEFRAMES + settings.LTF_TIMEFRAMES
        # Add daily candles for HTF campaign bias when enabled
        if settings.HTF_CAMPAIGN_ENABLED and "1d" not in all_timeframes:
            all_timeframes = all_timeframes + ["1d"]

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

        self._backfill_in_progress = False

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
                    self._postgres.store_funding_rate(fr)
            except Exception as e:
                logger.error(f"Initial funding rate fetch failed: pair={pair} error={e}")

            try:
                oi = self._exchange.fetch_open_interest(pair)
                if oi:
                    self._redis.set_open_interest(oi)
                    candle = self._ws_feed.get_latest_candle(pair, "5m")
                    current_price = candle.close if candle else 0.0
                    self._oi_proxy.update(oi, current_price)
                    self._postgres.store_open_interest(oi)
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

        # Persist CVD snapshot on every confirmed candle (for backtest history)
        try:
            cvd = self._cvd.get_cvd(candle.pair)
            if cvd:
                self._postgres.store_cvd_snapshot(cvd)
        except Exception as e:
            logger.error(f"Failed to store CVD snapshot: pair={candle.pair} error={e}")

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
            count_before = len(self._etherscan._movements)
            await self._etherscan._poll_all_wallets()
            self._publish_whale_movements()
            new_movements = self._etherscan._movements[count_before:]
            if new_movements:
                await self._notify_new_movements(new_movements)
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
            count_before = len(self._btc_whale._movements)
            await self._btc_whale._poll_all_wallets()
            self._publish_whale_movements()
            new_movements = self._btc_whale._movements[count_before:]
            if new_movements:
                await self._notify_new_movements(new_movements)
            await asyncio.sleep(settings.MEMPOOL_CHECK_INTERVAL)

    async def _notify_new_movements(self, movements: list) -> None:
        """Send whale alerts via AlertManager with strict filtering.

        Only exchange deposits/withdrawals above WHALE_NOTIFY_MIN_USD are sent
        to Telegram. All movements are still collected for AI context and dashboard.

        High significance bypasses whale batch and sends immediately.
        """
        if self._alert_manager is None:
            return
        for m in movements:
            # Skip neutral inter-wallet transfers (no directional signal)
            if settings.WHALE_NOTIFY_EXCHANGE_ONLY:
                if m.action not in ("exchange_deposit", "exchange_withdrawal"):
                    continue

            # Skip below USD minimum (small moves don't affect BTC/ETH price)
            if settings.WHALE_NOTIFY_MIN_USD > 0 and m.amount_usd < settings.WHALE_NOTIFY_MIN_USD:
                continue

            # Market makers: only notify on high significance
            if m.wallet.lower() in settings.MARKET_MAKER_WALLETS:
                if m.significance != "high":
                    continue

            try:
                immediate = m.significance == "high"
                await self._alert_manager.notify_whale_movement(m, immediate=immediate)
            except Exception as e:
                logger.error(f"Failed to send whale alert: {e}")

    def _publish_whale_movements(self) -> None:
        """Merge ETH + BTC whale movements and publish to Redis."""
        try:
            import json
            eth_records = self._etherscan.serialize_movements(hours=24)
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
                        self._postgres.store_funding_rate(fr)
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
                        # Get current price for OI flush side attribution
                        candle = self._ws_feed.get_latest_candle(pair, "5m")
                        current_price = candle.close if candle else 0.0
                        self._oi_proxy.update(oi, current_price)
                        self._postgres.store_open_interest(oi)
                except Exception as e:
                    logger.error(f"OI poll failed: pair={pair} error={e}")

    async def _news_sentiment_loop(self) -> None:
        """Poll news sentiment every NEWS_POLL_INTERVAL seconds."""
        if not settings.NEWS_SENTIMENT_ENABLED:
            logger.info("News sentiment: disabled in settings")
            return

        logger.info(f"News sentiment loop started: polling every {settings.NEWS_POLL_INTERVAL}s")

        # Initial fetch immediately
        try:
            sentiment = await self._news.fetch_sentiment()
            if sentiment:
                self._latest_sentiment = sentiment
                logger.info(f"News sentiment: F&G={sentiment.score} ({sentiment.label}), "
                            f"{len(sentiment.headlines)} headlines")
        except Exception as e:
            logger.error(f"Initial news sentiment fetch failed: {e}")

        while self._running:
            await asyncio.sleep(settings.NEWS_POLL_INTERVAL)
            try:
                sentiment = await self._news.fetch_sentiment()
                if sentiment:
                    self._latest_sentiment = sentiment
                    logger.debug(f"News sentiment: F&G={sentiment.score} ({sentiment.label})")
            except Exception as e:
                logger.error(f"News sentiment poll failed: {e}")

    # ================================================================
    # Metrics (Grafana)
    # ================================================================

    def _emit_metric(self, name: str, value: float, pair: str | None = None, labels: dict | None = None) -> None:
        """Write operational metric to PostgreSQL (fire-and-forget)."""
        try:
            self._postgres.insert_metric(name, value, pair=pair, labels=labels)
        except Exception:
            pass

    # ================================================================
    # Internal: Health check loop
    # ================================================================

    async def _health_check_loop(self) -> None:
        """Log health status every 30 seconds. Alert on state changes."""
        while self._running:
            await asyncio.sleep(30)
            status = self.health()
            disconnected = {k for k, v in status.items() if v is False}

            if disconnected:
                logger.warning(f"Health check: DOWN={list(disconnected)} state={self._state.name}")
            else:
                logger.debug(f"Health check: all systems OK state={self._state.name}")

            self._last_health_down = disconnected

            # Emit health metric: 1.0 = all OK, 0.0 = something down
            self._emit_metric("health_status", 0.0 if disconnected else 1.0)

            # Periodic cleanup of old metrics (~every 50 min)
            self._health_check_count += 1
            if self._health_check_count % 100 == 0:
                self._postgres.cleanup_old_metrics(retention_days=30)

    # ================================================================
    # Warmup check loop — transitions state to RUNNING
    # ================================================================

    async def _warmup_check_loop(self) -> None:
        """Runs every 10s. Calls _check_warmup() to evaluate state transitions."""
        while self._running:
            await asyncio.sleep(10)
            self._check_warmup()

    def _check_warmup(self) -> None:
        """Evaluate warmup conditions and transition state if ready.

        Extracted from the async loop so it can be tested synchronously.

        RUNNING requires:
        1. WS connected
        2. >= STARTUP_WARMUP_CANDLE_MIN candles per pair/tf
        3. At least 1 live WS candle received (not just backfill)
        4. Candle continuity (no gaps) in the last N candles per pair/tf
        5. Circuit breaker not tripped
        """
        # If DEGRADED (circuit breaker tripped), check for reset
        if self._state == DataServiceState.DEGRADED:
            if not self._circuit_breaker.is_tripped:
                logger.info("Circuit breaker reset — transitioning to RECOVERING")
                self._state = DataServiceState.RECOVERING
            return

        if self._state != DataServiceState.RECOVERING:
            return

        # Check warmup conditions
        ws_connected = self._ws_feed.is_connected
        if not ws_connected:
            return

        # Check candle count + continuity per pair/tf
        candles_ok = True
        continuity_ok = True
        min_count = settings.STARTUP_WARMUP_CANDLE_MIN
        for pair in settings.TRADING_PAIRS:
            for tf in settings.LTF_TIMEFRAMES:
                candles = self._ws_feed.get_candles(pair, tf, min_count)
                if len(candles) < min_count:
                    candles_ok = False
                    break

                # Validate continuity on the last min_count candles
                is_cont, gap_count = validate_candle_continuity(candles, tf)
                if not is_cont:
                    continuity_ok = False
                    logger.warning(
                        f"Warmup: candle gaps detected pair={pair} tf={tf} "
                        f"gaps={gap_count} candles={len(candles)}"
                    )
                    break
            if not candles_ok or not continuity_ok:
                break

        if not candles_ok or not continuity_ok:
            return

        # Require at least 1 live WS candle (not just backfilled data)
        if self._ws_feed._live_candle_count < 1:
            logger.debug("Warmup: waiting for first live WS candle")
            return

        # Check circuit breaker not tripped
        if self._circuit_breaker.is_tripped:
            self._state = DataServiceState.DEGRADED
            logger.critical("Circuit breaker tripped — state=DEGRADED")
            return

        # All checks pass — transition to RUNNING
        self._state = DataServiceState.RUNNING
        logger.info("Warmup complete — state=RUNNING")
        self._emit_metric("data_service_state", 1.0, labels={"state": "running"})

    # ================================================================
    # WS reconnect handler — gap backfill + circuit breaker
    # ================================================================

    async def _on_ws_reconnect(self) -> None:
        """Called by OKXWebSocketFeed after a successful reconnect."""
        logger.warning("WS reconnect detected — state=RECOVERING")
        self._state = DataServiceState.RECOVERING
        self._emit_metric("ws_reconnect", 1.0)

        # Record event in circuit breaker
        self._circuit_breaker.record_event()
        if self._circuit_breaker.is_tripped:
            self._state = DataServiceState.DEGRADED
            logger.critical(
                f"Circuit breaker TRIPPED: "
                f"{settings.CIRCUIT_BREAKER_MAX_RECONNECTS} reconnects in "
                f"{settings.CIRCUIT_BREAKER_WINDOW_SECONDS}s — state=DEGRADED"
            )
            self._emit_metric("circuit_breaker_tripped", 1.0)
            return

        # Trigger gap backfill (guarded by _backfill_in_progress)
        await self._gap_backfill()

    async def _gap_backfill(self) -> None:
        """Backfill candle gaps after a WS reconnect.

        Fetches up to 500 candles per pair/tf (paginated by exchange_client).
        If the gap is larger than what we can backfill, logs a critical warning
        and stays in RECOVERING — the warmup continuity check will catch it.
        """
        if self._backfill_in_progress:
            logger.warning("Gap backfill skipped — backfill already in progress")
            return

        self._backfill_in_progress = True
        logger.info("Gap backfill starting...")
        loop = asyncio.get_running_loop()
        all_timeframes = settings.HTF_TIMEFRAMES + settings.LTF_TIMEFRAMES
        unrecoverable_gaps = []

        try:
            for pair in settings.TRADING_PAIRS:
                for tf in all_timeframes:
                    try:
                        last_candle = self._ws_feed.get_latest_candle(pair, tf)
                        if not last_candle:
                            continue

                        # Calculate how many candles we're missing
                        now_ms = int(time.time() * 1000)
                        tf_ms = TIMEFRAME_MS.get(tf, 300_000)
                        gap_candles = (now_ms - last_candle.timestamp) // tf_ms

                        if gap_candles <= 0:
                            continue

                        # Fetch up to 500 candles (exchange_client paginates)
                        fetch_count = min(int(gap_candles) + 10, 500)
                        candles = await loop.run_in_executor(
                            None, self._exchange.backfill_candles, pair, tf, fetch_count
                        )

                        if not candles:
                            continue

                        new_candles = [c for c in candles if c.timestamp > last_candle.timestamp]
                        if not new_candles:
                            continue

                        self._ws_feed.store_candles(new_candles)
                        await loop.run_in_executor(
                            None, self._postgres.store_candles, new_candles
                        )

                        # Check if backfill covered the gap
                        is_cont, gap_count = validate_candle_continuity(
                            self._ws_feed.get_candles(pair, tf, settings.STARTUP_WARMUP_CANDLE_MIN),
                            tf,
                        )
                        if not is_cont:
                            unrecoverable_gaps.append((pair, tf, gap_count))

                        logger.info(
                            f"Gap backfill: pair={pair} tf={tf} "
                            f"gap_est={gap_candles} filled={len(new_candles)} "
                            f"continuous={is_cont}"
                        )

                    except Exception as e:
                        logger.error(f"Gap backfill failed: pair={pair} tf={tf} error={e}")

            if unrecoverable_gaps:
                logger.critical(
                    f"Gap backfill: {len(unrecoverable_gaps)} unrecoverable gaps detected. "
                    f"Staying in RECOVERING until gaps resolve or bot restarts. "
                    f"Gaps: {unrecoverable_gaps}"
                )
                self._emit_metric("gap_backfill_unrecoverable", float(len(unrecoverable_gaps)))
            else:
                logger.info("Gap backfill complete — all gaps filled")

        finally:
            self._backfill_in_progress = False
