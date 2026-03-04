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
from shared.models import Candle, FundingRate, OpenInterest

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
                    confidence DOUBLE PRECISION,
                    reasoning TEXT,
                    adjustments JSONB,
                    warnings JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS risk_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(30),
                    details JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            # Index for fast candle lookups
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_candles_pair_tf_ts
                ON candles(pair, timeframe, timestamp DESC)
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
        tp3_price: float,
        position_size: float,
        ai_confidence: float,
        actual_entry: float | None = None,
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

    def insert_ai_decision(
        self,
        trade_id: int | None,
        confidence: float,
        reasoning: str,
        adjustments: dict | None = None,
        warnings: list | None = None,
    ) -> int | None:
        """Insert an AI decision record. trade_id can be None for rejections."""
        for attempt in range(2):
            if not self._ensure_connected():
                return None
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO ai_decisions
                           (trade_id, confidence, reasoning, adjustments, warnings)
                           VALUES (%s, %s, %s, %s, %s)
                           RETURNING id""",
                        (trade_id, confidence, reasoning,
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

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            logger.info("PostgreSQL connection closed")
