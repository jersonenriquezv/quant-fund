"""
Data persistence layer — Redis (real-time cache) + PostgreSQL (historical).

Redis stores:
- Latest candle per pair/timeframe
- Latest funding rate, OI per pair
- Bot state: daily drawdown, cooldown timers, last processed timestamps

PostgreSQL stores:
- Historical candles (for backtesting and strategy warmup)
- Trade logs, AI decisions, risk events (future layers)

Schema matches CLAUDE.md "Database Schema" section exactly.
Redis is ONLY for state caching — NOT for inter-module messaging.
"""

import json
import time
from typing import Optional

import psycopg2
import psycopg2.extras
import redis

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, CVDSnapshot, FundingRate, OpenInterest

logger = setup_logger("data_service")


# ================================================================
# Redis key patterns
# ================================================================
# All keys prefixed with "qf:" (quant-fund) to avoid collisions

def _redis_key(category: str, *parts: str) -> str:
    return f"qf:{category}:{':'.join(parts)}"

# Examples:
# qf:candle:BTC/USDT:5m     → latest candle JSON
# qf:funding:BTC/USDT       → latest funding rate JSON
# qf:oi:BTC/USDT            → latest OI JSON
# qf:bot:daily_dd                → current daily drawdown float
# qf:bot:cooldown_until          → cooldown expiry timestamp
# qf:bot:last_candle_ts:BTC/USDT:5m → timestamp of last processed candle


class RedisStore:
    """Redis cache for real-time bot state.

    All values are JSON-serialized. TTLs prevent stale data from lingering.
    """

    def __init__(self):
        self._client: Optional[redis.Redis] = None

    def connect(self) -> bool:
        """Connect to Redis. Returns True if successful."""
        try:
            self._client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=5,
            )
            self._client.ping()
            logger.info(f"Redis connected: {settings.REDIS_HOST}:{settings.REDIS_PORT}")
            return True
        except redis.ConnectionError as e:
            logger.error(f"Redis connection failed: {settings.REDIS_HOST}:{settings.REDIS_PORT} "
                         f"error={e}")
            self._client = None
            return False

    @property
    def is_connected(self) -> bool:
        if not self._client:
            return False
        try:
            self._client.ping()
            return True
        except redis.ConnectionError:
            return False

    # --- Candles ---

    def set_latest_candle(self, candle: Candle) -> None:
        """Cache the latest confirmed candle for a pair/timeframe."""
        if not self._client:
            return
        key = _redis_key("candle", candle.pair, candle.timeframe)
        data = {
            "timestamp": candle.timestamp,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "volume_quote": candle.volume_quote,
            "pair": candle.pair,
            "timeframe": candle.timeframe,
            "confirmed": candle.confirmed,
        }
        self._client.set(key, json.dumps(data), ex=86400)  # 24h TTL

    def get_latest_candle(self, pair: str, timeframe: str) -> Optional[Candle]:
        """Get cached latest candle for a pair/timeframe."""
        if not self._client:
            return None
        key = _redis_key("candle", pair, timeframe)
        raw = self._client.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        return Candle(**data)

    # --- Funding Rate ---

    def set_funding_rate(self, fr: FundingRate) -> None:
        if not self._client:
            return
        key = _redis_key("funding", fr.pair)
        data = {
            "timestamp": fr.timestamp,
            "pair": fr.pair,
            "rate": fr.rate,
            "next_rate": fr.next_rate,
            "next_funding_time": fr.next_funding_time,
            "fetched_at": fr.fetched_at,
        }
        self._client.set(key, json.dumps(data), ex=32400)  # 9h TTL (>8h funding cycle)

    def get_funding_rate(self, pair: str) -> Optional[FundingRate]:
        if not self._client:
            return None
        key = _redis_key("funding", pair)
        raw = self._client.get(key)
        if not raw:
            return None
        return FundingRate(**json.loads(raw))

    # --- Open Interest ---

    def set_open_interest(self, oi: OpenInterest) -> None:
        if not self._client:
            return
        key = _redis_key("oi", oi.pair)
        data = {
            "timestamp": oi.timestamp,
            "pair": oi.pair,
            "oi_contracts": oi.oi_contracts,
            "oi_base": oi.oi_base,
            "oi_usd": oi.oi_usd,
        }
        self._client.set(key, json.dumps(data), ex=600)  # 10min TTL (polled every 5min)

    def get_open_interest(self, pair: str) -> Optional[OpenInterest]:
        if not self._client:
            return None
        key = _redis_key("oi", pair)
        raw = self._client.get(key)
        if not raw:
            return None
        return OpenInterest(**json.loads(raw))

    # --- Whale Movements ---

    def set_whale_movements(self, json_str: str) -> None:
        """Cache serialized whale movements. TTL 600s (polled every 5min)."""
        if not self._client:
            return
        key = _redis_key("bot", "whale_movements")
        self._client.set(key, json_str, ex=600)

    def get_whale_movements(self) -> Optional[str]:
        """Get cached whale movements JSON."""
        if not self._client:
            return None
        key = _redis_key("bot", "whale_movements")
        return self._client.get(key)

    # --- Bot State ---

    def set_bot_state(self, key_name: str, value: str, ttl: int = 86400) -> None:
        """Set arbitrary bot state (drawdown, cooldown, etc.)."""
        if not self._client:
            return
        key = _redis_key("bot", key_name)
        self._client.set(key, value, ex=ttl)

    def get_bot_state(self, key_name: str) -> Optional[str]:
        if not self._client:
            return None
        key = _redis_key("bot", key_name)
        return self._client.get(key)

    def set_positions(self, positions_json: str) -> None:
        """Cache current open positions as JSON."""
        if not self._client:
            return
        key = _redis_key("bot", "positions")
        self._client.set(key, positions_json, ex=86400)

    def get_positions(self) -> Optional[str]:
        """Get cached open positions JSON."""
        if not self._client:
            return None
        key = _redis_key("bot", "positions")
        return self._client.get(key)

    def set_last_candle_ts(self, pair: str, timeframe: str, timestamp: int) -> None:
        """Track timestamp of last processed candle per pair/tf."""
        if not self._client:
            return
        key = _redis_key("bot", "last_candle_ts", pair, timeframe)
        self._client.set(key, str(timestamp), ex=86400)

    def get_last_candle_ts(self, pair: str, timeframe: str) -> Optional[int]:
        if not self._client:
            return None
        key = _redis_key("bot", "last_candle_ts", pair, timeframe)
        raw = self._client.get(key)
        return int(raw) if raw else None

    def pop_cancel_request(self, pair: str) -> bool:
        """Check and consume a cancel request for a pair. Returns True if found."""
        if not self._client:
            return False
        key = f"qf:cancel_request:{pair}"
        val = self._client.get(key)
        if val is not None:
            self._client.delete(key)
            return True
        return False


class PostgresStore:
    """PostgreSQL for historical candle storage and trade logs.

    Schema matches CLAUDE.md "Database Schema" section.
    """

    def __init__(self):
        self._conn = None

    def connect(self) -> bool:
        """Connect to PostgreSQL and create tables if needed."""
        try:
            self._conn = psycopg2.connect(
                host=settings.POSTGRES_HOST,
                port=settings.POSTGRES_PORT,
                dbname=settings.POSTGRES_DB,
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD,
                connect_timeout=5,
            )
            self._conn.autocommit = True
            self._create_tables()
            logger.info(f"PostgreSQL connected: {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}"
                        f"/{settings.POSTGRES_DB}")
            return True
        except psycopg2.Error as e:
            logger.error(f"PostgreSQL connection failed: {settings.POSTGRES_HOST}:"
                         f"{settings.POSTGRES_PORT}/{settings.POSTGRES_DB} error={e}")
            self._conn = None
            return False

    @property
    def is_connected(self) -> bool:
        if not self._conn:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except psycopg2.Error:
            return False

    def _ensure_connected(self) -> bool:
        """Check connection and reconnect if stale/closed. Returns True if connected."""
        if self._conn:
            try:
                if not self._conn.closed:
                    with self._conn.cursor() as cur:
                        cur.execute("SELECT 1")
                    return True
            except psycopg2.Error:
                pass
            # Connection is stale/broken — close and reconnect
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        logger.warning("PostgreSQL connection lost — attempting reconnect")
        return self.connect()

    def _create_tables(self) -> None:
        """Create tables if they don't exist. Schema from CLAUDE.md."""
        with self._conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    id SERIAL PRIMARY KEY,
                    pair VARCHAR(20) NOT NULL,
                    timeframe VARCHAR(5) NOT NULL,
                    timestamp BIGINT NOT NULL,
                    open DOUBLE PRECISION NOT NULL,
                    high DOUBLE PRECISION NOT NULL,
                    low DOUBLE PRECISION NOT NULL,
                    close DOUBLE PRECISION NOT NULL,
                    volume DOUBLE PRECISION NOT NULL,
                    volume_quote DOUBLE PRECISION NOT NULL,
                    UNIQUE(pair, timeframe, timestamp)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    pair VARCHAR(20),
                    direction VARCHAR(5),
                    setup_type VARCHAR(10),
                    entry_price DOUBLE PRECISION,
                    sl_price DOUBLE PRECISION,
                    tp1_price DOUBLE PRECISION,
                    tp2_price DOUBLE PRECISION,
                    tp3_price DOUBLE PRECISION,
                    actual_entry DOUBLE PRECISION,
                    actual_exit DOUBLE PRECISION,
                    exit_reason VARCHAR(20),
                    position_size DOUBLE PRECISION,
                    pnl_usd DOUBLE PRECISION,
                    pnl_pct DOUBLE PRECISION,
                    ai_confidence DOUBLE PRECISION,
                    opened_at TIMESTAMP,
                    closed_at TIMESTAMP,
                    status VARCHAR(15) DEFAULT 'open'
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_decisions (
                    id SERIAL PRIMARY KEY,
                    trade_id INT REFERENCES trades(id),
                    pair VARCHAR(20),
                    direction VARCHAR(5),
                    setup_type VARCHAR(10),
                    approved BOOLEAN,
                    confidence DOUBLE PRECISION,
                    reasoning TEXT,
                    adjustments JSONB,
                    warnings JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Migration: add columns for existing databases
            for col, coltype in [
                ("pair", "VARCHAR(20)"),
                ("direction", "VARCHAR(5)"),
                ("setup_type", "VARCHAR(10)"),
                ("approved", "BOOLEAN"),
            ]:
                cur.execute(f"ALTER TABLE ai_decisions ADD COLUMN IF NOT EXISTS {col} {coltype}")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS risk_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(30),
                    details JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Drop redundant index — UNIQUE(pair, timeframe, timestamp) already covers lookups
            cur.execute("DROP INDEX IF EXISTS idx_candles_pair_tf_ts")

            # Dashboard queries: ORDER BY created_at DESC LIMIT N
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ai_decisions_created
                ON ai_decisions(created_at DESC)
            """)

            # Dashboard queries: WHERE status = $1 ORDER BY opened_at DESC
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_status_opened
                ON trades(status, opened_at DESC NULLS LAST)
            """)

            # Bot operational metrics (Grafana)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_metrics (
                    id SERIAL PRIMARY KEY,
                    metric_name VARCHAR(50) NOT NULL,
                    value DOUBLE PRECISION NOT NULL,
                    pair VARCHAR(20),
                    labels JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_name_time
                ON bot_metrics(metric_name, created_at DESC)
            """)

            # Historical funding rates and OI for backtesting
            cur.execute("""
                CREATE TABLE IF NOT EXISTS funding_rate_history (
                    id SERIAL PRIMARY KEY,
                    pair VARCHAR(20) NOT NULL,
                    timestamp BIGINT NOT NULL,
                    rate DOUBLE PRECISION NOT NULL,
                    next_rate DOUBLE PRECISION,
                    UNIQUE(pair, timestamp)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_funding_pair_ts
                ON funding_rate_history(pair, timestamp DESC)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS open_interest_history (
                    id SERIAL PRIMARY KEY,
                    pair VARCHAR(20) NOT NULL,
                    timestamp BIGINT NOT NULL,
                    oi_contracts DOUBLE PRECISION NOT NULL,
                    oi_base DOUBLE PRECISION NOT NULL,
                    oi_usd DOUBLE PRECISION NOT NULL,
                    UNIQUE(pair, timestamp)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_oi_pair_ts
                ON open_interest_history(pair, timestamp DESC)
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS cvd_history (
                    id SERIAL PRIMARY KEY,
                    pair VARCHAR(20) NOT NULL,
                    timestamp BIGINT NOT NULL,
                    cvd_5m DOUBLE PRECISION NOT NULL,
                    cvd_15m DOUBLE PRECISION NOT NULL,
                    cvd_1h DOUBLE PRECISION NOT NULL,
                    buy_volume DOUBLE PRECISION NOT NULL,
                    sell_volume DOUBLE PRECISION NOT NULL,
                    UNIQUE(pair, timestamp)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_cvd_pair_ts
                ON cvd_history(pair, timestamp DESC)
            """)

            # HTF campaigns table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS campaigns (
                    id SERIAL PRIMARY KEY,
                    campaign_id VARCHAR(20) NOT NULL,
                    pair VARCHAR(20) NOT NULL,
                    direction VARCHAR(5) NOT NULL,
                    initial_setup_type VARCHAR(10),
                    initial_entry_price DOUBLE PRECISION,
                    weighted_entry DOUBLE PRECISION,
                    total_size DOUBLE PRECISION,
                    total_margin DOUBLE PRECISION,
                    adds_count INT DEFAULT 0,
                    adds_detail JSONB,
                    current_sl_price DOUBLE PRECISION,
                    ai_confidence DOUBLE PRECISION,
                    htf_bias VARCHAR(10),
                    pnl_usd DOUBLE PRECISION,
                    pnl_pct DOUBLE PRECISION,
                    close_reason VARCHAR(20),
                    opened_at TIMESTAMP DEFAULT NOW(),
                    closed_at TIMESTAMP,
                    status VARCHAR(15) DEFAULT 'open',
                    UNIQUE(campaign_id)
                )
            """)

            # ML instrumentation — setup feature snapshots for future model training
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ml_setups (
                    id SERIAL PRIMARY KEY,
                    setup_id VARCHAR(20) NOT NULL UNIQUE,
                    feature_version INT NOT NULL DEFAULT 1,

                    -- Setup geometry
                    timestamp BIGINT NOT NULL,
                    pair VARCHAR(20) NOT NULL,
                    direction VARCHAR(5) NOT NULL,
                    setup_type VARCHAR(20) NOT NULL,
                    entry_price DOUBLE PRECISION NOT NULL,
                    sl_price DOUBLE PRECISION NOT NULL,
                    tp1_price DOUBLE PRECISION NOT NULL,
                    tp2_price DOUBLE PRECISION NOT NULL,
                    htf_bias VARCHAR(10),
                    ob_timeframe VARCHAR(5),

                    -- Derived geometry
                    risk_distance_pct DOUBLE PRECISION,
                    rr_ratio DOUBLE PRECISION,
                    entry_distance_pct DOUBLE PRECISION,
                    sl_distance_pct DOUBLE PRECISION,
                    current_price_at_detection DOUBLE PRECISION,
                    confluence_count INT,

                    -- Stale / late entry
                    setup_age_minutes DOUBLE PRECISION,

                    -- Decomposed confluences
                    has_liquidity_sweep BOOLEAN DEFAULT FALSE,
                    has_choch BOOLEAN DEFAULT FALSE,
                    has_bos BOOLEAN DEFAULT FALSE,
                    has_fvg BOOLEAN DEFAULT FALSE,
                    has_breaker_block BOOLEAN DEFAULT FALSE,
                    pd_zone VARCHAR(12),
                    pd_aligned BOOLEAN,
                    ob_volume_ratio DOUBLE PRECISION,
                    sweep_volume_ratio DOUBLE PRECISION,
                    has_oi_flush BOOLEAN DEFAULT FALSE,
                    oi_flush_usd DOUBLE PRECISION DEFAULT 0,
                    cvd_aligned BOOLEAN DEFAULT FALSE,
                    funding_extreme BOOLEAN DEFAULT FALSE,

                    -- Market state at detection
                    funding_rate DOUBLE PRECISION,
                    oi_usd DOUBLE PRECISION,
                    cvd_5m DOUBLE PRECISION,
                    cvd_15m DOUBLE PRECISION,
                    cvd_1h DOUBLE PRECISION,
                    buy_dominance DOUBLE PRECISION,
                    fear_greed_score INT,

                    -- Missingness flags
                    has_funding BOOLEAN DEFAULT FALSE,
                    has_oi BOOLEAN DEFAULT FALSE,
                    has_cvd BOOLEAN DEFAULT FALSE,
                    has_news BOOLEAN DEFAULT FALSE,
                    has_whales BOOLEAN DEFAULT FALSE,
                    whale_count INT DEFAULT 0,
                    recent_flush_count INT DEFAULT 0,
                    recent_flush_total_usd DOUBLE PRECISION DEFAULT 0,

                    -- Risk context (portfolio state)
                    risk_capital DOUBLE PRECISION,
                    risk_open_positions INT,
                    risk_daily_dd_pct DOUBLE PRECISION,
                    risk_weekly_dd_pct DOUBLE PRECISION,
                    risk_trades_today INT,

                    -- Setup H / momentum-specific features
                    impulse_move_pct DOUBLE PRECISION,
                    impulse_decel_ratio DOUBLE PRECISION,
                    impulse_vol_decay_ratio DOUBLE PRECISION,
                    impulse_directional_purity DOUBLE PRECISION,
                    has_initiating_ob BOOLEAN DEFAULT FALSE,

                    -- Graduated signal tiers (v5+)
                    sweep_tier VARCHAR(10),
                    funding_tier VARCHAR(10),
                    oi_rising_tier VARCHAR(10),
                    dominance_tier VARCHAR(10),
                    oi_delta_pct DOUBLE PRECISION,

                    -- Temporal / regime features (v5+)
                    hour_of_day INT,
                    atr_pct DOUBLE PRECISION,
                    daily_vol DOUBLE PRECISION,

                    -- Guardian close tracking
                    guardian_close_reason VARCHAR(30),

                    -- Guardian shadow triggers (AFML feature collection)
                    -- Set to TRUE the first time each check WOULD have fired
                    -- during the trade's lifetime. Used for feature importance.
                    guardian_shadow_counter BOOLEAN DEFAULT FALSE,
                    guardian_shadow_momentum BOOLEAN DEFAULT FALSE,
                    guardian_shadow_stall BOOLEAN DEFAULT FALSE,
                    guardian_shadow_cvd BOOLEAN DEFAULT FALSE,

                    -- Outcome (filled after trade resolves)
                    outcome_type VARCHAR(20),
                    pnl_pct DOUBLE PRECISION,
                    pnl_usd DOUBLE PRECISION,
                    actual_entry DOUBLE PRECISION,
                    actual_exit DOUBLE PRECISION,
                    exit_reason VARCHAR(20),
                    fill_duration_ms BIGINT,
                    trade_duration_ms BIGINT,

                    -- Metadata
                    created_at TIMESTAMP DEFAULT NOW(),
                    resolved_at TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ml_setups_pair_ts
                ON ml_setups(pair, timestamp DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ml_setups_outcome
                ON ml_setups(outcome_type) WHERE outcome_type IS NOT NULL
            """)

            # v6 migration: add daily_vol column if missing
            cur.execute("""
                ALTER TABLE ml_setups ADD COLUMN IF NOT EXISTS
                    daily_vol DOUBLE PRECISION
            """)

            # migration: add setup_id to trades for ML linkage
            cur.execute("""
                ALTER TABLE trades ADD COLUMN IF NOT EXISTS
                    setup_id VARCHAR(20)
            """)

        logger.info("PostgreSQL tables verified/created")

    # --- Candle Storage ---

    def store_candles(self, candles: list[Candle]) -> int:
        """Batch insert candles. Skips duplicates via ON CONFLICT.
        Returns number of candles actually inserted.
        """
        if not candles:
            return 0

        for attempt in range(2):
            if not self._ensure_connected():
                return 0
            try:
                values = [
                    (c.pair, c.timeframe, c.timestamp, c.open, c.high, c.low,
                     c.close, c.volume, c.volume_quote)
                    for c in candles
                ]
                with self._conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO candles (pair, timeframe, timestamp, open, high, low,
                                               close, volume, volume_quote)
                           VALUES %s
                           ON CONFLICT (pair, timeframe, timestamp) DO NOTHING""",
                        values,
                        page_size=100,
                    )
                    inserted = cur.rowcount
                logger.info(f"PostgreSQL: stored {inserted}/{len(candles)} candles "
                            f"(pair={candles[0].pair} tf={candles[0].timeframe})")
                return inserted
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL candle insert connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return 0
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL candle insert failed: {e}")
                return 0
        return 0

    def load_candles(self, pair: str, timeframe: str,
                     count: int = 500) -> list[Candle]:
        """Load last N candles from PostgreSQL. Returns oldest-first."""
        for attempt in range(2):
            if not self._ensure_connected():
                return []
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """SELECT timestamp, open, high, low, close, volume, volume_quote
                           FROM candles
                           WHERE pair = %s AND timeframe = %s
                           ORDER BY timestamp DESC
                           LIMIT %s""",
                        (pair, timeframe, count),
                    )
                    rows = cur.fetchall()

                candles = [
                    Candle(
                        timestamp=row[0], open=row[1], high=row[2], low=row[3],
                        close=row[4], volume=row[5], volume_quote=row[6],
                        pair=pair, timeframe=timeframe, confirmed=True,
                    )
                    for row in reversed(rows)  # Reverse to get oldest-first
                ]
                return candles
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL candle load connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return []
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL candle load failed: pair={pair} tf={timeframe} error={e}")
                return []
        return []

    # --- Trade Storage ---

    def insert_trade(
        self,
        pair: str,
        direction: str,
        setup_type: str,
        entry_price: float,
        sl_price: float,
        tp1_price: float,
        tp2_price: float,
        position_size: float,
        ai_confidence: float,
        actual_entry: float | None = None,
        tp3_price: float = 0.0,
        setup_id: str | None = None,
    ) -> int | None:
        """Insert a new trade record. Returns trade id or None on failure."""
        for attempt in range(2):
            if not self._ensure_connected():
                return None
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO trades
                           (pair, direction, setup_type, entry_price, sl_price,
                            tp1_price, tp2_price, tp3_price, position_size,
                            ai_confidence, actual_entry, setup_id, opened_at, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'open')
                           RETURNING id""",
                        (pair, direction, setup_type, entry_price, sl_price,
                         tp1_price, tp2_price, tp3_price, position_size,
                         ai_confidence, actual_entry, setup_id),
                    )
                    row = cur.fetchone()
                    trade_id = row[0] if row else None
                logger.info(f"PostgreSQL: inserted trade id={trade_id} {pair} {direction}")
                return trade_id
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL trade insert connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return None
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL trade insert failed: {e}")
                return None
        return None

    def update_trade(
        self,
        trade_id: int,
        actual_entry: float | None = None,
        actual_exit: float | None = None,
        exit_reason: str | None = None,
        pnl_usd: float | None = None,
        pnl_pct: float | None = None,
        status: str | None = None,
    ) -> bool:
        """Update an existing trade record. Only sets non-None fields."""
        if trade_id is None:
            return False

        fields = []
        values = []
        if actual_entry is not None:
            fields.append("actual_entry = %s")
            values.append(actual_entry)
        if actual_exit is not None:
            fields.append("actual_exit = %s")
            values.append(actual_exit)
        if exit_reason is not None:
            fields.append("exit_reason = %s")
            values.append(exit_reason)
        if pnl_usd is not None:
            fields.append("pnl_usd = %s")
            values.append(pnl_usd)
        if pnl_pct is not None:
            fields.append("pnl_pct = %s")
            values.append(pnl_pct)
        if status is not None:
            fields.append("status = %s")
            values.append(status)
            if status == "closed":
                fields.append("closed_at = NOW()")

        if not fields:
            return True

        values.append(trade_id)
        for attempt in range(2):
            if not self._ensure_connected():
                return False
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE trades SET {', '.join(fields)} WHERE id = %s",
                        values,
                    )
                logger.info(f"PostgreSQL: updated trade id={trade_id} status={status}")
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL trade update connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return False
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL trade update failed: id={trade_id} {e}")
                return False
        return False

    def fetch_open_trades(self) -> list[dict]:
        """Fetch all trades with status='open'. Returns list of dicts with id and pair."""
        for attempt in range(2):
            if not self._ensure_connected():
                return []
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, pair, direction, entry_price, opened_at, setup_id "
                        "FROM trades WHERE status = 'open'"
                    )
                    rows = cur.fetchall()
                return [
                    {"id": r[0], "pair": r[1], "direction": r[2],
                     "entry_price": r[3], "opened_at": r[4], "setup_id": r[5]}
                    for r in rows
                ]
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL fetch open trades connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return []
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL fetch open trades failed: {e}")
                return []

    def fetch_closed_trades_pnl(self, since_date: str, capital: float) -> dict:
        """Fetch aggregate PnL from closed trades since a given date.

        Args:
            since_date: ISO date string (e.g. '2026-03-19')
            capital: Current capital for pnl_pct calculation

        Returns dict with daily_pnl_pct, weekly_pnl_pct, trade_count.
        Used for drawdown reconciliation on restart.
        """
        result = {"daily_pnl_pct": 0.0, "weekly_pnl_pct": 0.0, "trade_count": 0}
        for attempt in range(2):
            if not self._ensure_connected():
                return result
            try:
                with self._conn.cursor() as cur:
                    # Daily: trades closed today
                    cur.execute(
                        "SELECT COALESCE(SUM(pnl_usd), 0), COUNT(*) "
                        "FROM trades WHERE status = 'closed' "
                        "AND closed_at >= %s::date",
                        (since_date,)
                    )
                    row = cur.fetchone()
                    daily_pnl_usd = float(row[0]) if row else 0.0
                    trade_count = int(row[1]) if row else 0
                    result["daily_pnl_pct"] = daily_pnl_usd / capital if capital > 0 else 0.0
                    result["trade_count"] = trade_count

                    # Weekly: trades closed this ISO week
                    cur.execute(
                        "SELECT COALESCE(SUM(pnl_usd), 0) "
                        "FROM trades WHERE status = 'closed' "
                        "AND EXTRACT(ISOYEAR FROM closed_at) = EXTRACT(ISOYEAR FROM CURRENT_DATE) "
                        "AND EXTRACT(WEEK FROM closed_at) = EXTRACT(WEEK FROM CURRENT_DATE)"
                    )
                    row = cur.fetchone()
                    weekly_pnl_usd = float(row[0]) if row else 0.0
                    result["weekly_pnl_pct"] = weekly_pnl_usd / capital if capital > 0 else 0.0

                return result
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL fetch closed trades pnl error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return result
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL fetch closed trades pnl failed: {e}")
                return result
        return result
        return []

    # --- ML Setup Storage ---

    def insert_ml_setup(
        self,
        setup_id: str,
        features: dict,
        risk_context: dict | None = None,
        feature_version: int = 1,
    ) -> bool:
        """Insert a feature snapshot for a detected setup. Fire-and-forget."""
        for attempt in range(2):
            if not self._ensure_connected():
                return False
            try:
                rc = risk_context or {}
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO ml_setups (
                            setup_id, feature_version, timestamp,
                            pair, direction, setup_type,
                            entry_price, sl_price, tp1_price, tp2_price,
                            htf_bias, ob_timeframe,
                            risk_distance_pct, rr_ratio, entry_distance_pct,
                            sl_distance_pct, current_price_at_detection,
                            confluence_count, setup_age_minutes,
                            has_liquidity_sweep, has_choch, has_bos,
                            has_fvg, has_breaker_block,
                            pd_zone, pd_aligned,
                            ob_volume_ratio, sweep_volume_ratio,
                            has_oi_flush, oi_flush_usd,
                            cvd_aligned, funding_extreme,
                            funding_rate, oi_usd,
                            cvd_5m, cvd_15m, cvd_1h,
                            buy_dominance, fear_greed_score,
                            has_funding, has_oi, has_cvd, has_news, has_whales,
                            whale_count, recent_flush_count, recent_flush_total_usd,
                            impulse_move_pct, impulse_decel_ratio,
                            impulse_vol_decay_ratio, impulse_directional_purity,
                            has_initiating_ob,
                            sweep_tier, funding_tier, oi_rising_tier,
                            dominance_tier, oi_delta_pct,
                            hour_of_day, atr_pct, daily_vol,
                            risk_capital, risk_open_positions,
                            risk_daily_dd_pct, risk_weekly_dd_pct, risk_trades_today
                        ) VALUES (
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s, %s
                        ) ON CONFLICT (setup_id) DO NOTHING""",
                        (
                            setup_id, feature_version, features.get("timestamp", 0),
                            features.get("pair"), features.get("direction"), features.get("setup_type"),
                            features.get("entry_price"), features.get("sl_price"),
                            features.get("tp1_price"), features.get("tp2_price"),
                            features.get("htf_bias"), features.get("ob_timeframe"),
                            features.get("risk_distance_pct"), features.get("rr_ratio"),
                            features.get("entry_distance_pct"),
                            features.get("sl_distance_pct"),
                            features.get("current_price_at_detection"),
                            features.get("confluence_count"), features.get("setup_age_minutes"),
                            features.get("has_liquidity_sweep"), features.get("has_choch"),
                            features.get("has_bos"),
                            features.get("has_fvg"), features.get("has_breaker_block"),
                            features.get("pd_zone"), features.get("pd_aligned"),
                            features.get("ob_volume_ratio"), features.get("sweep_volume_ratio"),
                            features.get("has_oi_flush"), features.get("oi_flush_usd"),
                            features.get("cvd_aligned"), features.get("funding_extreme"),
                            features.get("funding_rate"), features.get("oi_usd"),
                            features.get("cvd_5m"), features.get("cvd_15m"), features.get("cvd_1h"),
                            features.get("buy_dominance"), features.get("fear_greed_score"),
                            features.get("has_funding"), features.get("has_oi"),
                            features.get("has_cvd"), features.get("has_news"),
                            features.get("has_whales"),
                            features.get("whale_count"), features.get("recent_flush_count"),
                            features.get("recent_flush_total_usd"),
                            features.get("impulse_move_pct"), features.get("impulse_decel_ratio"),
                            features.get("impulse_vol_decay_ratio"),
                            features.get("impulse_directional_purity"),
                            features.get("has_initiating_ob", False),
                            features.get("sweep_tier"), features.get("funding_tier"),
                            features.get("oi_rising_tier"),
                            features.get("dominance_tier"), features.get("oi_delta_pct"),
                            features.get("hour_of_day"), features.get("atr_pct"),
                            features.get("daily_vol"),
                            rc.get("risk_capital"), rc.get("risk_open_positions"),
                            rc.get("risk_daily_dd_pct"), rc.get("risk_weekly_dd_pct"),
                            rc.get("risk_trades_today"),
                        ),
                    )
                logger.debug(f"ML: inserted setup {setup_id} ({features.get('pair')} {features.get('setup_type')})")
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"ML setup insert connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return False
            except psycopg2.Error as e:
                logger.error(f"ML setup insert failed: setup_id={setup_id} {e}")
                return False
        return False

    def update_ml_setup_outcome(
        self,
        setup_id: str,
        outcome_type: str,
        pnl_pct: float | None = None,
        pnl_usd: float | None = None,
        actual_entry: float | None = None,
        actual_exit: float | None = None,
        exit_reason: str | None = None,
        fill_duration_ms: int | None = None,
        trade_duration_ms: int | None = None,
        risk_context: dict | None = None,
        guardian_reason: str | None = None,
    ) -> bool:
        """Update outcome columns for an ml_setup row. Fire-and-forget."""
        for attempt in range(2):
            if not self._ensure_connected():
                return False
            try:
                fields = ["outcome_type = %s", "resolved_at = NOW()"]
                values: list = [outcome_type]

                if pnl_pct is not None:
                    fields.append("pnl_pct = %s")
                    values.append(pnl_pct)
                if pnl_usd is not None:
                    fields.append("pnl_usd = %s")
                    values.append(pnl_usd)
                if actual_entry is not None:
                    fields.append("actual_entry = %s")
                    values.append(actual_entry)
                if actual_exit is not None:
                    fields.append("actual_exit = %s")
                    values.append(actual_exit)
                if exit_reason is not None:
                    fields.append("exit_reason = %s")
                    values.append(exit_reason)
                if fill_duration_ms is not None:
                    fields.append("fill_duration_ms = %s")
                    values.append(fill_duration_ms)
                if trade_duration_ms is not None:
                    fields.append("trade_duration_ms = %s")
                    values.append(trade_duration_ms)
                if guardian_reason is not None:
                    fields.append("guardian_close_reason = %s")
                    values.append(guardian_reason)
                # Risk context can be added at risk check time
                if risk_context:
                    for key in ("risk_capital", "risk_open_positions",
                                "risk_daily_dd_pct", "risk_weekly_dd_pct",
                                "risk_trades_today"):
                        if key in risk_context:
                            fields.append(f"{key} = %s")
                            values.append(risk_context[key])

                values.append(setup_id)
                with self._conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE ml_setups SET {', '.join(fields)} WHERE setup_id = %s",
                        values,
                    )
                logger.debug(f"ML: updated setup {setup_id} outcome={outcome_type}")
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"ML setup update connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return False
            except psycopg2.Error as e:
                logger.error(f"ML setup outcome update failed: setup_id={setup_id} {e}")
                return False
        return False

    def update_ml_guardian_shadow(
        self, setup_id: str, check_name: str
    ) -> bool:
        """Record that a guardian shadow check WOULD have triggered.

        Sets the corresponding boolean column to TRUE (idempotent).
        Only writes once per (setup_id, check_name) — subsequent calls are no-ops
        because the column is already TRUE.

        Args:
            setup_id: The ML setup ID from ManagedPosition.
            check_name: One of "counter", "momentum", "stall", "cvd".
        """
        column_map = {
            "counter": "guardian_shadow_counter",
            "momentum": "guardian_shadow_momentum",
            "stall": "guardian_shadow_stall",
            "cvd": "guardian_shadow_cvd",
        }
        column = column_map.get(check_name)
        if not column:
            return False
        if not self._ensure_connected():
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f"UPDATE ml_setups SET {column} = TRUE "
                    f"WHERE setup_id = %s AND {column} = FALSE",
                    (setup_id,),
                )
            return True
        except psycopg2.Error as e:
            logger.error(f"ML guardian shadow update failed: {setup_id} {check_name} {e}")
            return False

    def count_ml_training_outcomes(self, min_version: int = 4) -> dict:
        """Count labeled outcomes suitable for ML training.

        Returns dict with outcome counts and total, e.g.:
        {"filled_tp": 12, "filled_sl": 8, "filled_trailing": 3, "total": 23}
        """
        if not self._ensure_connected():
            return {"total": 0}
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT outcome_type, COUNT(*) FROM ml_setups
                       WHERE feature_version >= %s
                       AND outcome_type IN ('filled_tp', 'filled_sl', 'filled_trailing')
                       GROUP BY outcome_type""",
                    (min_version,),
                )
                rows = cur.fetchall()
                result = {row[0]: row[1] for row in rows}
                result["total"] = sum(result.values())
                return result
        except Exception as e:
            logger.error(f"ML training count query failed: {e}")
            return {"total": 0}

    # --- Campaign Storage ---

    def insert_campaign(self, campaign) -> int | None:
        """Insert a new HTF campaign record. Returns DB id or None."""
        import json as _json
        for attempt in range(2):
            if not self._ensure_connected():
                return None
            try:
                adds_detail = _json.dumps([{
                    "add_number": a.add_number,
                    "margin": a.margin,
                    "entry_price": a.entry_price,
                    "actual_entry_price": a.actual_entry_price,
                    "size": a.size,
                    "filled": a.filled,
                } for a in campaign.adds]) if campaign.adds else "[]"

                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO campaigns
                           (campaign_id, pair, direction, initial_setup_type,
                            initial_entry_price, weighted_entry, total_size,
                            total_margin, adds_count, adds_detail,
                            current_sl_price, ai_confidence, htf_bias,
                            opened_at, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'open')
                           RETURNING id""",
                        (campaign.campaign_id, campaign.pair, campaign.direction,
                         campaign.initial_setup_type, campaign.initial_entry_price,
                         campaign.weighted_entry, campaign.total_size,
                         campaign.total_margin, len(campaign.adds), adds_detail,
                         campaign.current_sl_price, campaign.ai_confidence,
                         campaign.htf_bias),
                    )
                    row = cur.fetchone()
                    db_id = row[0] if row else None
                logger.info(f"PostgreSQL: inserted campaign id={db_id} {campaign.pair}")
                return db_id
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL campaign insert error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return None
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL campaign insert failed: {e}")
                return None
        return None

    def update_campaign(self, campaign) -> bool:
        """Update a campaign record on close."""
        import json as _json
        db_id = campaign.db_campaign_id
        if db_id is None:
            return False

        adds_detail = _json.dumps([{
            "add_number": a.add_number,
            "margin": a.margin,
            "entry_price": a.entry_price,
            "actual_entry_price": a.actual_entry_price,
            "size": a.size,
            "filled": a.filled,
        } for a in campaign.adds]) if campaign.adds else "[]"

        for attempt in range(2):
            if not self._ensure_connected():
                return False
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """UPDATE campaigns SET
                           weighted_entry = %s, total_size = %s, total_margin = %s,
                           adds_count = %s, adds_detail = %s, current_sl_price = %s,
                           pnl_usd = %s, pnl_pct = %s, close_reason = %s,
                           closed_at = NOW(), status = 'closed'
                           WHERE id = %s""",
                        (campaign.weighted_entry, campaign.total_size,
                         campaign.total_margin, len(campaign.adds), adds_detail,
                         campaign.current_sl_price, campaign.pnl_usd,
                         campaign.pnl_pct, campaign.close_reason, db_id),
                    )
                logger.info(f"PostgreSQL: updated campaign id={db_id} status=closed")
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL campaign update error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return False
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL campaign update failed: {e}")
                return False
        return False

    def insert_ai_decision(
        self,
        trade_id: int | None,
        confidence: float,
        reasoning: str,
        adjustments: dict | None = None,
        warnings: list | None = None,
        pair: str | None = None,
        direction: str | None = None,
        setup_type: str | None = None,
        approved: bool | None = None,
    ) -> int | None:
        """Insert an AI decision record. trade_id can be None for rejections."""
        for attempt in range(2):
            if not self._ensure_connected():
                return None
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO ai_decisions
                           (trade_id, pair, direction, setup_type, approved,
                            confidence, reasoning, adjustments, warnings)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           RETURNING id""",
                        (trade_id, pair, direction, setup_type, approved,
                         confidence, reasoning,
                         json.dumps(adjustments or {}),
                         json.dumps(warnings or [])),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL ai_decision insert connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return None
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL ai_decision insert failed: {e}")
                return None
        return None

    def insert_risk_event(
        self, event_type: str, details: dict
    ) -> int | None:
        """Insert a risk event record."""
        for attempt in range(2):
            if not self._ensure_connected():
                return None
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO risk_events (event_type, details)
                           VALUES (%s, %s) RETURNING id""",
                        (event_type, json.dumps(details)),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL risk_event insert connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return None
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL risk_event insert failed: {e}")
                return None
        return None

    # --- Bot Metrics (Grafana) ---

    def insert_metric(
        self, name: str, value: float,
        pair: str | None = None, labels: dict | None = None,
    ) -> None:
        """Insert an operational metric (fire-and-forget)."""
        for attempt in range(2):
            if not self._ensure_connected():
                return
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO bot_metrics (metric_name, value, pair, labels)
                           VALUES (%s, %s, %s, %s)""",
                        (name, value, pair,
                         json.dumps(labels) if labels else None),
                    )
                return
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL metric insert connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL metric insert failed: {e}")
                return

    def cleanup_old_metrics(self, retention_days: int = 30) -> int:
        """Delete metrics older than retention_days. Returns rows deleted."""
        for attempt in range(2):
            if not self._ensure_connected():
                return 0
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bot_metrics WHERE created_at < NOW() - INTERVAL '%s days'",
                        (retention_days,),
                    )
                    deleted = cur.rowcount
                if deleted > 0:
                    logger.info(f"PostgreSQL: cleaned up {deleted} old metrics (>{retention_days}d)")
                return deleted
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL metric cleanup connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return 0
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL metric cleanup failed: {e}")
                return 0
        return 0

    # --- Funding Rate History ---

    def store_funding_rate(self, fr: FundingRate) -> None:
        """Store a single funding rate snapshot for backtesting."""
        for attempt in range(2):
            if not self._ensure_connected():
                return
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO funding_rate_history (pair, timestamp, rate, next_rate)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (pair, timestamp) DO NOTHING""",
                        (fr.pair, fr.timestamp, fr.rate, fr.next_rate),
                    )
                return
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL funding store connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL funding store failed: {e}")
                return

    def store_funding_rates_batch(self, rates: list[tuple]) -> int:
        """Batch insert funding rates. rates = [(pair, ts, rate, next_rate), ...].
        Returns number inserted."""
        if not rates:
            return 0
        for attempt in range(2):
            if not self._ensure_connected():
                return 0
            try:
                with self._conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO funding_rate_history (pair, timestamp, rate, next_rate)
                           VALUES %s ON CONFLICT (pair, timestamp) DO NOTHING""",
                        rates,
                        page_size=100,
                    )
                    inserted = cur.rowcount
                return inserted
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL funding batch connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return 0
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL funding batch failed: {e}")
                return 0
        return 0

    def load_funding_rates(self, pair: str, since_ms: int = 0,
                           until_ms: int = 0) -> list[FundingRate]:
        """Load historical funding rates for a pair, oldest-first."""
        for attempt in range(2):
            if not self._ensure_connected():
                return []
            try:
                with self._conn.cursor() as cur:
                    if until_ms > 0:
                        cur.execute(
                            """SELECT timestamp, rate, next_rate
                               FROM funding_rate_history
                               WHERE pair = %s AND timestamp >= %s AND timestamp <= %s
                               ORDER BY timestamp ASC""",
                            (pair, since_ms, until_ms),
                        )
                    else:
                        cur.execute(
                            """SELECT timestamp, rate, next_rate
                               FROM funding_rate_history
                               WHERE pair = %s AND timestamp >= %s
                               ORDER BY timestamp ASC""",
                            (pair, since_ms),
                        )
                    rows = cur.fetchall()
                return [
                    FundingRate(
                        timestamp=row[0], pair=pair, rate=row[1],
                        next_rate=row[2] or 0.0, next_funding_time=0,
                    )
                    for row in rows
                ]
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL funding load connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return []
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL funding load failed: {e}")
                return []
        return []

    # --- Open Interest History ---

    def store_open_interest(self, oi: OpenInterest) -> None:
        """Store a single OI snapshot for backtesting."""
        for attempt in range(2):
            if not self._ensure_connected():
                return
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO open_interest_history
                           (pair, timestamp, oi_contracts, oi_base, oi_usd)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (pair, timestamp) DO NOTHING""",
                        (oi.pair, oi.timestamp, oi.oi_contracts, oi.oi_base, oi.oi_usd),
                    )
                return
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL OI store connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL OI store failed: {e}")
                return

    def store_open_interest_batch(self, records: list[tuple]) -> int:
        """Batch insert OI records. records = [(pair, ts, contracts, base, usd), ...].
        Returns number inserted."""
        if not records:
            return 0
        for attempt in range(2):
            if not self._ensure_connected():
                return 0
            try:
                with self._conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO open_interest_history
                           (pair, timestamp, oi_contracts, oi_base, oi_usd)
                           VALUES %s ON CONFLICT (pair, timestamp) DO NOTHING""",
                        records,
                        page_size=100,
                    )
                    inserted = cur.rowcount
                return inserted
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL OI batch connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return 0
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL OI batch failed: {e}")
                return 0
        return 0

    def load_open_interest(self, pair: str, since_ms: int = 0,
                           until_ms: int = 0) -> list[OpenInterest]:
        """Load historical OI for a pair, oldest-first."""
        for attempt in range(2):
            if not self._ensure_connected():
                return []
            try:
                with self._conn.cursor() as cur:
                    if until_ms > 0:
                        cur.execute(
                            """SELECT timestamp, oi_contracts, oi_base, oi_usd
                               FROM open_interest_history
                               WHERE pair = %s AND timestamp >= %s AND timestamp <= %s
                               ORDER BY timestamp ASC""",
                            (pair, since_ms, until_ms),
                        )
                    else:
                        cur.execute(
                            """SELECT timestamp, oi_contracts, oi_base, oi_usd
                               FROM open_interest_history
                               WHERE pair = %s AND timestamp >= %s
                               ORDER BY timestamp ASC""",
                            (pair, since_ms),
                        )
                    rows = cur.fetchall()
                return [
                    OpenInterest(
                        timestamp=row[0], pair=pair, oi_contracts=row[1],
                        oi_base=row[2], oi_usd=row[3],
                    )
                    for row in rows
                ]
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL OI load connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return []
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL OI load failed: {e}")
                return []
        return []

    # --- CVD History ---

    def store_cvd_snapshot(self, cvd: CVDSnapshot) -> None:
        """Store a CVD snapshot for backtesting. Called every 5s per pair."""
        for attempt in range(2):
            if not self._ensure_connected():
                return
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO cvd_history
                           (pair, timestamp, cvd_5m, cvd_15m, cvd_1h,
                            buy_volume, sell_volume)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (pair, timestamp) DO NOTHING""",
                        (cvd.pair, cvd.timestamp, cvd.cvd_5m, cvd.cvd_15m,
                         cvd.cvd_1h, cvd.buy_volume, cvd.sell_volume),
                    )
                return
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL CVD store connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL CVD store failed: {e}")
                return

    def load_cvd_snapshots(self, pair: str, since_ms: int = 0,
                           until_ms: int = 0) -> list[CVDSnapshot]:
        """Load historical CVD snapshots for a pair, oldest-first."""
        for attempt in range(2):
            if not self._ensure_connected():
                return []
            try:
                with self._conn.cursor() as cur:
                    if until_ms > 0:
                        cur.execute(
                            """SELECT timestamp, cvd_5m, cvd_15m, cvd_1h,
                                      buy_volume, sell_volume
                               FROM cvd_history
                               WHERE pair = %s AND timestamp >= %s AND timestamp <= %s
                               ORDER BY timestamp ASC""",
                            (pair, since_ms, until_ms),
                        )
                    else:
                        cur.execute(
                            """SELECT timestamp, cvd_5m, cvd_15m, cvd_1h,
                                      buy_volume, sell_volume
                               FROM cvd_history
                               WHERE pair = %s AND timestamp >= %s
                               ORDER BY timestamp ASC""",
                            (pair, since_ms),
                        )
                    rows = cur.fetchall()
                return [
                    CVDSnapshot(
                        timestamp=row[0], pair=pair, cvd_5m=row[1],
                        cvd_15m=row[2], cvd_1h=row[3],
                        buy_volume=row[4], sell_volume=row[5],
                    )
                    for row in rows
                ]
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"PostgreSQL CVD load connection error (attempt {attempt+1}): {e}")
                self._conn = None
                if attempt == 1:
                    return []
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL CVD load failed: {e}")
                return []
        return []

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            logger.info("PostgreSQL connection closed")
