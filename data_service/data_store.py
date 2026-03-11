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
                            ai_confidence, actual_entry, opened_at, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'open')
                           RETURNING id""",
                        (pair, direction, setup_type, entry_price, sl_price,
                         tp1_price, tp2_price, tp3_price, position_size,
                         ai_confidence, actual_entry),
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
                        "SELECT id, pair, direction, entry_price, opened_at "
                        "FROM trades WHERE status = 'open'"
                    )
                    rows = cur.fetchall()
                return [
                    {"id": r[0], "pair": r[1], "direction": r[2],
                     "entry_price": r[3], "opened_at": r[4]}
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
        return []

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
