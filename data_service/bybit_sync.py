"""Bybit read-only trade log sync.

Fetches executions (fills) and closed PnL from Bybit UTA via read-only API,
stores in Postgres tables separate from bot trades. Used to build a manual
trade log for setups traded by hand on Bybit.

Idempotent — uses Bybit execId / (orderId, updatedTime) as unique keys.
Safe to run repeatedly.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import execute_batch
from pybit.unified_trading import HTTP

from config.settings import Settings
from shared.logger import setup_logger

logger = setup_logger("bybit_sync")

CATEGORIES = ("linear", "spot", "inverse")


def _ms_to_ts(ms: str | int | None) -> datetime | None:
    if ms in (None, "", "0"):
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class BybitSync:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        if not self.settings.BYBIT_API_KEY or not self.settings.BYBIT_API_SECRET:
            raise RuntimeError("BYBIT_API_KEY / BYBIT_API_SECRET missing")
        self.client = HTTP(
            testnet=self.settings.BYBIT_TESTNET,
            api_key=self.settings.BYBIT_API_KEY,
            api_secret=self.settings.BYBIT_API_SECRET,
        )

    def _conn(self):
        return psycopg2.connect(
            host=self.settings.POSTGRES_HOST,
            port=self.settings.POSTGRES_PORT,
            dbname=self.settings.POSTGRES_DB,
            user=self.settings.POSTGRES_USER,
            password=self.settings.POSTGRES_PASSWORD,
        )

    def ensure_tables(self) -> None:
        ddl_executions = """
        CREATE TABLE IF NOT EXISTS bybit_executions (
            exec_id VARCHAR(64) PRIMARY KEY,
            order_id VARCHAR(64),
            symbol VARCHAR(20),
            side VARCHAR(10),
            order_type VARCHAR(20),
            exec_qty DOUBLE PRECISION,
            exec_price DOUBLE PRECISION,
            exec_value DOUBLE PRECISION,
            exec_fee DOUBLE PRECISION,
            fee_rate DOUBLE PRECISION,
            is_maker BOOLEAN,
            closed_size DOUBLE PRECISION,
            leaves_qty DOUBLE PRECISION,
            category VARCHAR(10),
            exec_time TIMESTAMPTZ,
            synced_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_bybit_exec_time ON bybit_executions(exec_time DESC);
        CREATE INDEX IF NOT EXISTS idx_bybit_exec_symbol ON bybit_executions(symbol);
        """
        ddl_pnl = """
        CREATE TABLE IF NOT EXISTS bybit_closed_pnl (
            id BIGSERIAL PRIMARY KEY,
            order_id VARCHAR(64),
            symbol VARCHAR(20),
            side VARCHAR(10),
            qty DOUBLE PRECISION,
            avg_entry_price DOUBLE PRECISION,
            avg_exit_price DOUBLE PRECISION,
            closed_pnl DOUBLE PRECISION,
            cum_entry_value DOUBLE PRECISION,
            cum_exit_value DOUBLE PRECISION,
            leverage DOUBLE PRECISION,
            exec_type VARCHAR(20),
            category VARCHAR(10),
            created_time TIMESTAMPTZ,
            updated_time TIMESTAMPTZ,
            synced_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(order_id, updated_time)
        );
        CREATE INDEX IF NOT EXISTS idx_bybit_pnl_updated ON bybit_closed_pnl(updated_time DESC);
        CREATE INDEX IF NOT EXISTS idx_bybit_pnl_symbol ON bybit_closed_pnl(symbol);
        """
        ddl_annotations = """
        CREATE TABLE IF NOT EXISTS bybit_trade_annotations (
            id BIGSERIAL PRIMARY KEY,
            order_id VARCHAR(64),
            position_idx SMALLINT DEFAULT 0,
            symbol VARCHAR(20) NOT NULL,
            side VARCHAR(10) NOT NULL,
            opened_at TIMESTAMPTZ NOT NULL,
            entry_price DOUBLE PRECISION,
            size DOUBLE PRECISION,
            leverage DOUBLE PRECISION,
            notional_value DOUBLE PRECISION,
            -- user annotations (filled later via mobile form)
            setup_type VARCHAR(30),
            confluences JSONB,
            confidence SMALLINT,
            thesis_pre TEXT,
            lesson_post TEXT,
            emotional_state VARCHAR(30),
            grade_self CHAR(1),
            screenshot_url TEXT,
            -- context snapshot at entry (auto)
            context_snapshot JSONB,
            -- auto-classification from snapshot (set at entry)
            auto_setup_type VARCHAR(30),
            auto_confluences JSONB,
            auto_detractors JSONB,
            auto_grade CHAR(1),
            auto_classifier_version SMALLINT,
            -- outcome (filled from bybit_closed_pnl match)
            closed_at TIMESTAMPTZ,
            exit_price DOUBLE PRECISION,
            pnl_usd DOUBLE PRECISION,
            pnl_pct DOUBLE PRECISION,
            pnl_r DOUBLE PRECISION,
            closed_pnl_id BIGINT,
            status VARCHAR(15) DEFAULT 'open',
            -- timestamps
            annotated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(symbol, side, opened_at)
        );
        CREATE INDEX IF NOT EXISTS idx_bybit_annot_opened ON bybit_trade_annotations(opened_at DESC);
        CREATE INDEX IF NOT EXISTS idx_bybit_annot_status ON bybit_trade_annotations(status);
        CREATE INDEX IF NOT EXISTS idx_bybit_annot_symbol ON bybit_trade_annotations(symbol);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_setup_type VARCHAR(30);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_confluences JSONB;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_detractors JSONB;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_grade CHAR(1);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_classifier_version SMALLINT;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS trigger_condition TEXT;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS thesis_invalidation TEXT;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS topdown_brief_used BOOLEAN;

        -- ============================================================
        -- Journal v2 (redesign 2026-05-30) — additive, never drops v1.
        -- v1 rows keep journal_schema_version=1 (frozen, excluded from
        -- new edge math). v2 writers set 2. See SYSTEM_BASELINE §Bybit.
        -- ============================================================
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS journal_schema_version SMALLINT DEFAULT 1;

        -- PLAN: top-down chain (closed vocab; human label, may disagree with auto_*).
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS htf_bias_daily VARCHAR(10);       -- bullish/bearish/range
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS htf_bias_4h VARCHAR(10);          -- bullish/bearish/range
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS htf_structure_reason VARCHAR(15); -- HH_HL/LH_LL/range_bound/unclear
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS location_pd VARCHAR(12);          -- premium/equilibrium/discount
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS location_quality VARCHAR(12);     -- key_level/no_mans_land
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS mtf_1h VARCHAR(12);               -- confirms/contradicts/neutral
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS ltf_trigger VARCHAR(15);          -- sweep_reclaim/bos/choch/fvg/order_block/simple_break
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS structure_type VARCHAR(12);       -- continuation/reversal/range
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS entry_type VARCHAR(20);           -- at_level_limit/confirmation_shift

        -- PLAN: 5 independent confluence factors (booleans) -> derived count.
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS conf_htf BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS conf_location BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS conf_mtf BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS conf_trigger BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS conf_noconflict BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS tf_aligned_count SMALLINT
            GENERATED ALWAYS AS (
                COALESCE(conf_htf::int,0) + COALESCE(conf_location::int,0) + COALESCE(conf_mtf::int,0)
                + COALESCE(conf_trigger::int,0) + COALESCE(conf_noconflict::int,0)
            ) STORED;

        -- Phase 3 (2026-06-01): auto-classifier v2 chain pre-fill. Immutable machine
        -- prediction stored alongside the human cols above; human/machine disagreement
        -- IS the misread signal — never overwrite. trade_classifier.CLASSIFIER_VERSION=2.
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_htf_bias_daily VARCHAR(10);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_htf_bias_4h VARCHAR(10);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_htf_structure_reason VARCHAR(15);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_location_pd VARCHAR(12);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_location_quality VARCHAR(12);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_mtf_1h VARCHAR(12);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_ltf_trigger VARCHAR(15);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_structure_type VARCHAR(12);
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_conf_htf BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_conf_location BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_conf_mtf BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_conf_trigger BOOLEAN;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS auto_conf_noconflict BOOLEAN;

        -- PLAN: intended levels (R unit = |planned_entry - planned_sl| * size).
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS planned_entry_price DOUBLE PRECISION;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS planned_sl_price DOUBLE PRECISION;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS planned_tp_price DOUBLE PRECISION;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS risk_pct DOUBLE PRECISION;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS account_equity_at_open DOUBLE PRECISION;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS position_sl_price DOUBLE PRECISION;  -- actual SL from get_positions

        -- REVIEW: process diagnosis (the ML clean-sample label).
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS followed_process BOOLEAN;  -- NULL = not reviewed
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS technical_error JSONB;     -- array of tags; '[]' = reviewed, none
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS behavioral_error JSONB;    -- array of tags; '[]' = reviewed, none

        -- REVIEW: excursion + R metrics (filled by scripts/compute_bybit_mae_mfe.py).
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS mae_r DOUBLE PRECISION;            -- worst adverse excursion in R (<=0)
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS mfe_r DOUBLE PRECISION;            -- best favorable excursion in R (>=0)
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS realized_r DOUBLE PRECISION;       -- closed_pnl / R_usd
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS exit_efficiency DOUBLE PRECISION;  -- realized_r / mfe_r (NULL if mfe_r<=0)
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS entry_slippage_bps DOUBLE PRECISION;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS mae_mfe_tf VARCHAR(5);             -- candle granularity used (1m/5m)

        -- Exit-discipline + planning flags (2026-06-25 trade-log review).
        -- took_partial / moved_to_be are DERIVED at close by the watcher (rows_counted>1,
        -- final stop at/beyond entry). planned_tp1/tp2 + is_practice are human/manual.
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS took_partial BOOLEAN;  -- scaled out (>1 close row)
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS moved_to_be BOOLEAN;   -- stop pulled to break-even
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS is_practice BOOLEAN;    -- micro "get hands dirty" trade — excluded from edge math
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS planned_tp1 DOUBLE PRECISION;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS planned_tp2 DOUBLE PRECISION;

        -- REVIEW: derived labels.
        -- clean_sample = the edge-math gate. A practice (micro-size) trade is NOT a
        -- clean sample even if rules were followed — its size is unrepresentative, so it
        -- must stay out of expectancy/PF/exit-efficiency (2026-06-25 review). Fresh DBs
        -- get the is_practice clause inline; existing DBs are migrated by the DO block below.
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS clean_sample BOOLEAN
            GENERATED ALWAYS AS (
                followed_process IS TRUE
                AND behavioral_error = '[]'::jsonb
                AND is_practice IS NOT TRUE
            ) STORED;
        -- Recreate clean_sample on existing DBs whose generated expr predates the
        -- is_practice clause (a generation expression cannot be ALTERed in place).
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_attrdef d
                JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
                WHERE a.attrelid = 'bybit_trade_annotations'::regclass
                  AND a.attname = 'clean_sample'
                  AND pg_get_expr(d.adbin, d.adrelid) LIKE '%is_practice%'
            ) THEN
                DROP INDEX IF EXISTS idx_bybit_annot_clean;
                ALTER TABLE bybit_trade_annotations DROP COLUMN IF EXISTS clean_sample;
                ALTER TABLE bybit_trade_annotations ADD COLUMN clean_sample BOOLEAN
                    GENERATED ALWAYS AS (
                        followed_process IS TRUE
                        AND behavioral_error = '[]'::jsonb
                        AND is_practice IS NOT TRUE
                    ) STORED;
            END IF;
        END $$;
        ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS trade_quality VARCHAR(10)
            GENERATED ALWAYS AS (
                CASE
                    WHEN followed_process IS NULL OR realized_r IS NULL THEN NULL
                    WHEN followed_process AND realized_r > 0 THEN 'good_win'
                    WHEN followed_process AND realized_r <= 0 THEN 'good_loss'
                    WHEN realized_r > 0 THEN 'bad_win'
                    ELSE 'bad_loss'
                END
            ) STORED;
        CREATE INDEX IF NOT EXISTS idx_bybit_annot_jsv ON bybit_trade_annotations(journal_schema_version);
        CREATE INDEX IF NOT EXISTS idx_bybit_annot_clean ON bybit_trade_annotations(clean_sample);
        """
        ddl_pending = """
        CREATE TABLE IF NOT EXISTS bybit_pending_orders (
            id BIGSERIAL PRIMARY KEY,
            order_id VARCHAR(64) UNIQUE NOT NULL,
            order_link_id VARCHAR(64),
            symbol VARCHAR(20) NOT NULL,
            side VARCHAR(10) NOT NULL,
            order_type VARCHAR(20),
            qty DOUBLE PRECISION,
            price DOUBLE PRECISION,
            trigger_price DOUBLE PRECISION,
            stop_order_type VARCHAR(30),
            time_in_force VARCHAR(20),
            reduce_only BOOLEAN,
            position_idx SMALLINT DEFAULT 0,
            category VARCHAR(10) DEFAULT 'linear',
            -- state machine
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            placed_at TIMESTAMPTZ NOT NULL,
            filled_at TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ,
            -- pre-annotation (user fills via mobile form BEFORE fill)
            setup_type VARCHAR(30),
            confluences JSONB,
            confidence SMALLINT,
            thesis_pre TEXT,
            emotional_state VARCHAR(30),
            screenshot_url TEXT,
            context_snapshot JSONB,
            auto_setup_type VARCHAR(30),
            auto_confluences JSONB,
            auto_detractors JSONB,
            auto_grade CHAR(1),
            auto_classifier_version SMALLINT,
            -- link to resulting annotation when order fills
            annotation_id BIGINT REFERENCES bybit_trade_annotations(id) ON DELETE SET NULL,
            placed_to_fill_sec INT,
            placed_to_cancel_sec INT,
            last_seen_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_bybit_pending_status ON bybit_pending_orders(status);
        CREATE INDEX IF NOT EXISTS idx_bybit_pending_symbol ON bybit_pending_orders(symbol);
        CREATE INDEX IF NOT EXISTS idx_bybit_pending_placed ON bybit_pending_orders(placed_at DESC);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_setup_type VARCHAR(30);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_confluences JSONB;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_detractors JSONB;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_grade CHAR(1);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_classifier_version SMALLINT;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS enforcement_cancelled_at TIMESTAMPTZ;

        -- ============================================================
        -- Journal v2 (redesign 2026-05-30) — PLAN layer on pending orders.
        -- Chain captured BEFORE fill; migrates to annotation on fill.
        -- ============================================================
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS journal_schema_version SMALLINT DEFAULT 1;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS htf_bias_daily VARCHAR(10);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS htf_bias_4h VARCHAR(10);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS htf_structure_reason VARCHAR(15);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS location_pd VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS location_quality VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS mtf_1h VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS ltf_trigger VARCHAR(15);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS structure_type VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS entry_type VARCHAR(20);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS conf_htf BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS conf_location BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS conf_mtf BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS conf_trigger BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS conf_noconflict BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS tf_aligned_count SMALLINT
            GENERATED ALWAYS AS (
                COALESCE(conf_htf::int,0) + COALESCE(conf_location::int,0) + COALESCE(conf_mtf::int,0)
                + COALESCE(conf_trigger::int,0) + COALESCE(conf_noconflict::int,0)
            ) STORED;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS planned_entry_price DOUBLE PRECISION;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS planned_sl_price DOUBLE PRECISION;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS planned_tp_price DOUBLE PRECISION;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS risk_pct DOUBLE PRECISION;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS account_equity_at_open DOUBLE PRECISION;

        -- Phase 3 (2026-06-01): auto-classifier v2 chain pre-fill (mirrors annotations).
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_htf_bias_daily VARCHAR(10);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_htf_bias_4h VARCHAR(10);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_htf_structure_reason VARCHAR(15);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_location_pd VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_location_quality VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_mtf_1h VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_ltf_trigger VARCHAR(15);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_structure_type VARCHAR(12);
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_conf_htf BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_conf_location BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_conf_mtf BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_conf_trigger BOOLEAN;
        ALTER TABLE bybit_pending_orders ADD COLUMN IF NOT EXISTS auto_conf_noconflict BOOLEAN;
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(ddl_executions)
            cur.execute(ddl_pnl)
            cur.execute(ddl_annotations)
            cur.execute(ddl_pending)
            conn.commit()
        logger.info("bybit_sync: tables ensured")

    def _paginate(self, fn, category: str, **kwargs) -> Iterable[dict]:
        cursor = ""
        pages = 0
        while True:
            params = dict(kwargs, category=category, limit=100)
            if cursor:
                params["cursor"] = cursor
            resp = fn(**params)
            result = resp.get("result", {}) or {}
            rows = result.get("list", []) or []
            for row in rows:
                yield row
            cursor = result.get("nextPageCursor") or ""
            pages += 1
            if not cursor or pages >= 50:
                break
            time.sleep(0.1)

    def _time_windows(self, days: int, chunk_days: int = 7) -> list[tuple[int, int]]:
        """Split `days` into chunks that fit Bybit's 7-day max per request."""
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 86400 * 1000
        chunk_ms = chunk_days * 86400 * 1000
        windows: list[tuple[int, int]] = []
        cursor = start_ms
        while cursor < end_ms:
            w_end = min(cursor + chunk_ms, end_ms)
            windows.append((cursor, w_end))
            cursor = w_end
        return windows

    def sync_executions(self, category: str = "linear", days: int = 7) -> int:
        rows: list[tuple] = []
        for start_ms, end_ms in self._time_windows(days):
            for r in self._paginate(
                self.client.get_executions,
                category=category,
                startTime=start_ms,
                endTime=end_ms,
            ):
                rows.append((
                    r.get("execId"),
                    r.get("orderId"),
                    r.get("symbol"),
                    r.get("side"),
                    r.get("orderType"),
                    _to_float(r.get("execQty")),
                    _to_float(r.get("execPrice")),
                    _to_float(r.get("execValue")),
                    _to_float(r.get("execFee")),
                    _to_float(r.get("feeRate")),
                    r.get("isMaker") in (True, "true", "True", 1, "1"),
                    _to_float(r.get("closedSize")),
                    _to_float(r.get("leavesQty")),
                    category,
                    _ms_to_ts(r.get("execTime")),
                ))
        if not rows:
            logger.info(f"bybit_sync: no executions for {category} last {days}d")
            return 0
        sql = """
        INSERT INTO bybit_executions (
            exec_id, order_id, symbol, side, order_type,
            exec_qty, exec_price, exec_value, exec_fee, fee_rate,
            is_maker, closed_size, leaves_qty, category, exec_time
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (exec_id) DO NOTHING
        """
        with self._conn() as conn, conn.cursor() as cur:
            execute_batch(cur, sql, rows, page_size=200)
            conn.commit()
        logger.info(f"bybit_sync: upserted {len(rows)} executions ({category})")
        return len(rows)

    def sync_closed_pnl(self, category: str = "linear", days: int = 7) -> int:
        rows: list[tuple] = []
        for start_ms, end_ms in self._time_windows(days):
            for r in self._paginate(
                self.client.get_closed_pnl,
                category=category,
                startTime=start_ms,
                endTime=end_ms,
            ):
                rows.append((
                    r.get("orderId"),
                    r.get("symbol"),
                    r.get("side"),
                    _to_float(r.get("qty")),
                    _to_float(r.get("avgEntryPrice")),
                    _to_float(r.get("avgExitPrice")),
                    _to_float(r.get("closedPnl")),
                    _to_float(r.get("cumEntryValue")),
                    _to_float(r.get("cumExitValue")),
                    _to_float(r.get("leverage")),
                    r.get("execType"),
                    category,
                    _ms_to_ts(r.get("createdTime")),
                    _ms_to_ts(r.get("updatedTime")),
                ))
        if not rows:
            logger.info(f"bybit_sync: no closed PnL for {category} last {days}d")
            return 0
        sql = """
        INSERT INTO bybit_closed_pnl (
            order_id, symbol, side, qty, avg_entry_price, avg_exit_price,
            closed_pnl, cum_entry_value, cum_exit_value, leverage,
            exec_type, category, created_time, updated_time
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (order_id, updated_time) DO NOTHING
        """
        with self._conn() as conn, conn.cursor() as cur:
            execute_batch(cur, sql, rows, page_size=200)
            conn.commit()
        logger.info(f"bybit_sync: upserted {len(rows)} closed PnL ({category})")
        return len(rows)

    def sync_all(self, days: int = 7, categories: tuple[str, ...] = ("linear",)) -> dict[str, int]:
        self.ensure_tables()
        counts: dict[str, int] = {}
        for cat in categories:
            counts[f"{cat}_executions"] = self.sync_executions(category=cat, days=days)
            counts[f"{cat}_closed_pnl"] = self.sync_closed_pnl(category=cat, days=days)
        return counts
