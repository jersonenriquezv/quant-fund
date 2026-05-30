"""Bybit position watcher daemon.

Polls open positions via REST every N seconds. Detects transitions:
    - opened (new position appeared)
    - closed (position disappeared)
    - modified (size changed — pyramid add or partial TP)

On each open event:
    - Inserts empty annotation row in bybit_trade_annotations
    - Captures context snapshot (HTF bias, funding, OI delta, CVD, liq clusters)
    - Sends Telegram alert with link to annotation form

On each close event:
    - Updates annotation row with pnl + exit context
    - Sends Telegram closure alert with PnL + link to post-mortem

Run as standalone daemon. One-shot check via --once flag.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_batch
from pybit.unified_trading import HTTP

from config.settings import settings
from shared.logger import setup_logger

logger = setup_logger("bybit_watcher")

POLL_INTERVAL_SEC = 60
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass(frozen=True)
class PositionKey:
    symbol: str
    side: str  # Buy / Sell
    position_idx: int = 0


@dataclass
class PositionState:
    key: PositionKey
    size: float
    entry_price: float
    leverage: float
    updated_at: datetime
    raw: dict = field(default_factory=dict)


@dataclass
class PendingOrder:
    order_id: str
    order_link_id: str
    symbol: str
    side: str
    order_type: str
    qty: float
    price: float
    trigger_price: float | None
    stop_order_type: str
    time_in_force: str
    reduce_only: bool
    position_idx: int
    placed_at: datetime
    raw: dict = field(default_factory=dict)


class BybitWatcher:
    def __init__(self) -> None:
        if not settings.BYBIT_API_KEY or not settings.BYBIT_API_SECRET:
            raise RuntimeError("BYBIT_API_KEY / BYBIT_API_SECRET missing")
        self.client = HTTP(
            testnet=settings.BYBIT_TESTNET,
            api_key=settings.BYBIT_API_KEY,
            api_secret=settings.BYBIT_API_SECRET,
        )
        self._last_state: dict[PositionKey, PositionState] = {}
        self._last_pending: dict[str, PendingOrder] = {}
        self._dashboard_base = os.getenv(
            "DASHBOARD_PUBLIC_URL", "http://100.120.181.11:3000"
        )
        # Ensure schema is current — idempotent, safe to call on every startup.
        # Prevents missing-column errors after deploys that ship migrations.
        try:
            from data_service.bybit_sync import BybitSync
            BybitSync().ensure_tables()
        except Exception as exc:
            logger.warning(f"ensure_tables on startup failed: {exc}")
        # Hydrate in-memory state from DB so restarts don't silence the diff.
        # Without this, the first post-restart tick sees empty prev state and
        # treated any existing pending/positions as "bootstrap" (no alert),
        # dropping notifications for orders placed while the watcher was down.
        try:
            self._last_pending = self._load_pending_from_db()
            self._last_state = self._load_positions_from_db()
            if self._last_pending or self._last_state:
                logger.info(
                    f"bybit_watcher: hydrated from DB — "
                    f"{len(self._last_state)} position(s), {len(self._last_pending)} pending"
                )
        except Exception as exc:
            logger.warning(f"hydrate_state_from_db failed: {exc}")

    def _conn(self):
        return psycopg2.connect(
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT,
            dbname=settings.POSTGRES_DB,
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
        )

    def _fetch_positions(self) -> dict[PositionKey, PositionState]:
        """Return current open positions keyed by (symbol, side, position_idx)."""
        state: dict[PositionKey, PositionState] = {}
        resp = self.client.get_positions(category="linear", settleCoin="USDT")
        rows = (resp.get("result") or {}).get("list", []) or []
        for r in rows:
            size = float(r.get("size") or 0)
            if size <= 0:
                continue
            key = PositionKey(
                symbol=r.get("symbol"),
                side=r.get("side"),
                position_idx=int(r.get("positionIdx") or 0),
            )
            state[key] = PositionState(
                key=key,
                size=size,
                entry_price=float(r.get("avgPrice") or 0),
                leverage=float(r.get("leverage") or 0),
                updated_at=datetime.now(tz=timezone.utc),
                raw=r,
            )
        return state

    def _fetch_pending(self) -> dict[str, PendingOrder]:
        """Return current open (pending/untriggered) orders keyed by order_id."""
        out: dict[str, PendingOrder] = {}
        for order_filter in ("Order", "StopOrder"):
            try:
                resp = self.client.get_open_orders(
                    category="linear",
                    settleCoin="USDT",
                    orderFilter=order_filter,
                )
            except Exception as exc:
                logger.warning(f"get_open_orders ({order_filter}) failed: {exc}")
                continue
            rows = (resp.get("result") or {}).get("list", []) or []
            for r in rows:
                oid = r.get("orderId")
                if not oid:
                    continue
                placed = datetime.fromtimestamp(int(r.get("createdTime") or 0) / 1000, tz=timezone.utc) \
                    if r.get("createdTime") else datetime.now(tz=timezone.utc)
                out[oid] = PendingOrder(
                    order_id=oid,
                    order_link_id=r.get("orderLinkId") or "",
                    symbol=r.get("symbol") or "",
                    side=r.get("side") or "",
                    order_type=r.get("orderType") or "",
                    qty=float(r.get("qty") or 0),
                    price=float(r.get("price") or 0),
                    trigger_price=float(r["triggerPrice"]) if r.get("triggerPrice") else None,
                    stop_order_type=r.get("stopOrderType") or "",
                    time_in_force=r.get("timeInForce") or "",
                    reduce_only=bool(r.get("reduceOnly")),
                    position_idx=int(r.get("positionIdx") or 0),
                    placed_at=placed,
                    raw=r,
                )
        return out

    def _load_pending_from_db(self) -> dict[str, PendingOrder]:
        """Reconstruct prior in-memory pending state from DB on startup."""
        out: dict[str, PendingOrder] = {}
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT order_id, order_link_id, symbol, side, order_type,
                       qty, price, trigger_price, stop_order_type, time_in_force,
                       reduce_only, position_idx, placed_at
                FROM bybit_pending_orders
                WHERE status = 'pending'
                """
            )
            for r in cur.fetchall():
                oid = r["order_id"]
                out[oid] = PendingOrder(
                    order_id=oid,
                    order_link_id=r.get("order_link_id") or "",
                    symbol=r.get("symbol") or "",
                    side=r.get("side") or "",
                    order_type=r.get("order_type") or "",
                    qty=float(r.get("qty") or 0),
                    price=float(r.get("price") or 0),
                    trigger_price=float(r["trigger_price"]) if r.get("trigger_price") is not None else None,
                    stop_order_type=r.get("stop_order_type") or "",
                    time_in_force=r.get("time_in_force") or "",
                    reduce_only=bool(r.get("reduce_only")),
                    position_idx=int(r.get("position_idx") or 0),
                    placed_at=r["placed_at"],
                    raw={},
                )
        return out

    def _load_positions_from_db(self) -> dict[PositionKey, PositionState]:
        """Reconstruct prior in-memory position state from open annotations."""
        out: dict[PositionKey, PositionState] = {}
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (symbol, side)
                    symbol, side, opened_at, entry_price, size, leverage
                FROM bybit_trade_annotations
                WHERE status = 'open'
                ORDER BY symbol, side, opened_at DESC
                """
            )
            for r in cur.fetchall():
                key = PositionKey(
                    symbol=r.get("symbol") or "",
                    side=r.get("side") or "",
                    position_idx=0,  # not stored in annotation; OneWay mode = 0
                )
                out[key] = PositionState(
                    key=key,
                    size=float(r.get("size") or 0),
                    entry_price=float(r.get("entry_price") or 0),
                    leverage=float(r.get("leverage") or 0),
                    updated_at=r["opened_at"],
                    raw={},
                )
        return out

    async def _send_telegram(self, text: str) -> None:
        token = settings.TELEGRAM_BOT_TOKEN
        chat_id = settings.TELEGRAM_CHAT_ID
        if not token or not chat_id:
            logger.warning(f"[no-telegram] {text}")
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    TELEGRAM_API.format(token=token),
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
        except Exception as exc:
            logger.error(f"telegram send failed: {exc}")

    def _context_snapshot(self, key: PositionKey) -> dict[str, Any]:
        """Lightweight context snapshot. Full version implemented in context_service."""
        try:
            from data_service.context_service import build_context_snapshot
            return build_context_snapshot(key.symbol, key.side)
        except Exception as exc:
            logger.warning(f"context snapshot failed for {key.symbol}: {exc}")
            return {"error": str(exc)}

    def _classify(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Run deterministic auto-classifier on snapshot. Empty dict on error."""
        try:
            from strategy_service.trade_classifier import classify
            return classify(snapshot)
        except Exception as exc:
            logger.warning(f"classify failed: {exc}")
            return {}

    def _upsert_pending(self, p: PendingOrder, context: dict, auto: dict) -> int:
        sql = """
        INSERT INTO bybit_pending_orders (
            order_id, order_link_id, symbol, side, order_type,
            qty, price, trigger_price, stop_order_type, time_in_force,
            reduce_only, position_idx, placed_at, context_snapshot,
            auto_setup_type, auto_confluences, auto_detractors,
            auto_grade, auto_classifier_version,
            status, last_seen_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',NOW(),NOW())
        ON CONFLICT (order_id) DO UPDATE SET
            qty = EXCLUDED.qty,
            price = EXCLUDED.price,
            trigger_price = EXCLUDED.trigger_price,
            last_seen_at = NOW(),
            updated_at = NOW()
        RETURNING id
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (
                p.order_id, p.order_link_id, p.symbol, p.side, p.order_type,
                p.qty, p.price, p.trigger_price, p.stop_order_type, p.time_in_force,
                p.reduce_only, p.position_idx, p.placed_at, Json(context),
                auto.get("auto_setup_type"),
                Json(auto.get("auto_confluences")) if auto.get("auto_confluences") is not None else None,
                Json(auto.get("auto_detractors")) if auto.get("auto_detractors") is not None else None,
                auto.get("auto_grade"),
                auto.get("auto_classifier_version"),
            ))
            row = cur.fetchone()
            conn.commit()
            return row[0]

    def _resolve_pending(self, order_id: str, terminal: str) -> dict | None:
        """Mark pending order as filled/cancelled. Returns row snapshot for thesis carry-forward."""
        terminal_col = "filled_at" if terminal == "filled" else "cancelled_at"
        delta_col = "placed_to_fill_sec" if terminal == "filled" else "placed_to_cancel_sec"
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE bybit_pending_orders
                SET status = %s,
                    {terminal_col} = NOW(),
                    {delta_col} = EXTRACT(EPOCH FROM (NOW() - placed_at))::INT,
                    updated_at = NOW()
                WHERE order_id = %s AND status = 'pending'
                RETURNING *
                """,
                (terminal, order_id),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None

    def _link_annotation_from_pending(
        self, symbol: str, side: str, annotation_id: int,
        size: float | None = None, entry_price: float | None = None,
    ) -> dict | None:
        """When a position opens, try to carry forward the most recent pending
        order's thesis. Prefers qty+price proximity over pure time heuristic
        so two similar pendings in the same window don't cross-link.

        Strategy (best match wins):
          1. Filled within 10 min, annotation unattached.
          2. Rank by qty_diff / entry_price_diff (smaller = better). Ties
             broken by most-recent filled_at.
          3. If neither size nor entry_price available, fall back to the
             legacy most-recent-in-5-min heuristic.
        """
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            if size is None or entry_price is None or entry_price <= 0:
                cur.execute(
                    """
                    SELECT * FROM bybit_pending_orders
                    WHERE symbol = %s AND side = %s
                      AND status = 'filled'
                      AND filled_at >= NOW() - INTERVAL '5 minutes'
                      AND annotation_id IS NULL
                    ORDER BY filled_at DESC LIMIT 1
                    """,
                    (symbol, side),
                )
                pending = cur.fetchone()
            else:
                cur.execute(
                    """
                    SELECT *,
                           ABS(COALESCE(qty, 0) - %s) / NULLIF(GREATEST(%s, 1e-9), 0) AS qty_rel,
                           ABS(COALESCE(price, 0) - %s) / NULLIF(%s, 0) AS price_rel
                    FROM bybit_pending_orders
                    WHERE symbol = %s AND side = %s
                      AND status = 'filled'
                      AND filled_at >= NOW() - INTERVAL '10 minutes'
                      AND annotation_id IS NULL
                    ORDER BY (
                        COALESCE(ABS(COALESCE(qty, 0) - %s) / NULLIF(GREATEST(%s, 1e-9), 0), 1.0)
                      + COALESCE(ABS(COALESCE(price, 0) - %s) / NULLIF(%s, 0), 1.0)
                    ) ASC,
                    filled_at DESC LIMIT 1
                    """,
                    (size, size, entry_price, entry_price,
                     symbol, side,
                     size, size, entry_price, entry_price),
                )
                pending = cur.fetchone()
                # Guard: reject match if either dimension is way off (>20%).
                # Prevents wrong-link when only one pending exists but it is
                # clearly a different trade (e.g. resized after edit).
                if pending is not None:
                    qty_rel = pending.get("qty_rel")
                    price_rel = pending.get("price_rel")
                    if ((qty_rel is not None and qty_rel > 0.20)
                            or (price_rel is not None and price_rel > 0.02)):
                        logger.info(
                            f"bybit link: rejected pending {pending.get('order_id')} "
                            f"for {symbol} {side} — qty_rel={qty_rel} price_rel={price_rel}"
                        )
                        pending = None
            if not pending:
                return None
            # migrate thesis
            cur.execute(
                """
                UPDATE bybit_trade_annotations
                SET setup_type = COALESCE(setup_type, %s),
                    confluences = COALESCE(confluences, %s),
                    confidence = COALESCE(confidence, %s),
                    thesis_pre = COALESCE(thesis_pre, %s),
                    emotional_state = COALESCE(emotional_state, %s),
                    screenshot_url = COALESCE(screenshot_url, %s),
                    annotated_at = COALESCE(annotated_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    pending["setup_type"],
                    Json(pending["confluences"]) if pending["confluences"] is not None else None,
                    pending["confidence"],
                    pending["thesis_pre"],
                    pending["emotional_state"],
                    pending["screenshot_url"],
                    annotation_id,
                ),
            )
            cur.execute(
                "UPDATE bybit_pending_orders SET annotation_id = %s WHERE id = %s",
                (annotation_id, pending["id"]),
            )
            conn.commit()
            return dict(pending)

    def _insert_annotation(self, st: PositionState, context: dict, auto: dict) -> int:
        notional = st.size * st.entry_price
        # Journal v2 data sources: actual protective SL (R-unit denominator) and
        # account equity (risk_pct / sizing-consistency denominator), captured at
        # open. SL may still be NULL here if attached seconds later — _refresh_sl
        # syncs it on the next tick.
        sl_raw = st.raw.get("stopLoss")
        sl_price = float(sl_raw) if sl_raw not in (None, "", "0") else None
        equity = self._get_equity()
        sql = """
        INSERT INTO bybit_trade_annotations (
            symbol, side, opened_at, entry_price, size, leverage,
            notional_value, context_snapshot,
            auto_setup_type, auto_confluences, auto_detractors,
            auto_grade, auto_classifier_version,
            position_sl_price, account_equity_at_open, journal_schema_version,
            status
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
        ON CONFLICT (symbol, side, opened_at) DO UPDATE
            SET context_snapshot = EXCLUDED.context_snapshot,
                auto_setup_type = EXCLUDED.auto_setup_type,
                auto_confluences = EXCLUDED.auto_confluences,
                auto_detractors = EXCLUDED.auto_detractors,
                auto_grade = EXCLUDED.auto_grade,
                auto_classifier_version = EXCLUDED.auto_classifier_version,
                position_sl_price = COALESCE(EXCLUDED.position_sl_price, bybit_trade_annotations.position_sl_price),
                account_equity_at_open = COALESCE(EXCLUDED.account_equity_at_open, bybit_trade_annotations.account_equity_at_open),
                journal_schema_version = EXCLUDED.journal_schema_version
        RETURNING id
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (
                st.key.symbol, st.key.side, st.updated_at,
                st.entry_price, st.size, st.leverage,
                notional, Json(context),
                auto.get("auto_setup_type"),
                Json(auto.get("auto_confluences")) if auto.get("auto_confluences") is not None else None,
                Json(auto.get("auto_detractors")) if auto.get("auto_detractors") is not None else None,
                auto.get("auto_grade"),
                auto.get("auto_classifier_version"),
                sl_price, equity, 2,
            ))
            row = cur.fetchone()
            conn.commit()
            return row[0]

    def _get_equity(self) -> float | None:
        """UNIFIED account total equity (USD) at trade-open time.

        Denominator for risk_pct / sizing-consistency analysis. Best-effort:
        returns None on any API failure so an open is never blocked by it.
        """
        try:
            resp = self.client.get_wallet_balance(accountType="UNIFIED")
            rows = (resp.get("result") or {}).get("list", []) or []
            if not rows:
                return None
            total = rows[0].get("totalEquity")
            return float(total) if total not in (None, "") else None
        except Exception as exc:
            logger.warning(f"get_wallet_balance failed (equity not captured): {exc}")
            return None

    def _refresh_sl(self, st: PositionState) -> None:
        """Sync position_sl_price on the open annotation when the live SL changes.

        Bybit users routinely open first and attach/trail the stop seconds later,
        so capturing only at open stores NULL/stale SLs and breaks the R unit.
        """
        sl_raw = st.raw.get("stopLoss")
        sl_price = float(sl_raw) if sl_raw not in (None, "", "0") else None
        if sl_price is None:
            return
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bybit_trade_annotations
                    SET position_sl_price = %s, updated_at = NOW()
                    WHERE symbol = %s AND side = %s AND status = 'open'
                      AND position_sl_price IS DISTINCT FROM %s
                    """,
                    (sl_price, st.key.symbol, st.key.side, sl_price),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"refresh_sl failed for {st.key.symbol} {st.key.side}: {exc}")

    def _backfill_daily_candles(self) -> None:
        """REST-fetch recent 1D candles for the manual-bias symbols into `candles`.

        The trading bot's WebSocket only stores 5m/15m/1h/4h, so the Daily anchor
        of the manual top-down chain has no source otherwise. 1D candles move
        slowly, so a periodic REST refresh is sufficient. Idempotent via
        ON CONFLICT; best-effort (never blocks the poll loop).

        Bybit kline rows: [startMs, open, high, low, close, volume(base), turnover(quote)].
        """
        from data_service.context_service import bybit_symbol_to_pair
        symbols = getattr(settings, "BYBIT_DAILY_BIAS_SYMBOLS", []) or []
        rows: list[tuple] = []
        for symbol in symbols:
            try:
                resp = self.client.get_kline(
                    category="linear", symbol=symbol, interval="D", limit=40
                )
                klines = (resp.get("result") or {}).get("list", []) or []
            except Exception as exc:
                logger.warning(f"daily kline fetch failed for {symbol}: {exc}")
                continue
            pair = bybit_symbol_to_pair(symbol)
            if not pair:
                continue
            for k in klines:
                try:
                    rows.append((
                        pair, "1d", int(k[0]),
                        float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                        float(k[5]), float(k[6]),
                    ))
                except (TypeError, ValueError, IndexError):
                    continue
        if not rows:
            return
        try:
            with self._conn() as conn, conn.cursor() as cur:
                execute_batch(
                    cur,
                    """
                    INSERT INTO candles
                      (pair, timeframe, timestamp, open, high, low, close, volume, volume_quote)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (pair, timeframe, timestamp) DO NOTHING
                    """,
                    rows,
                )
                conn.commit()
            logger.info(f"bybit daily bias: upserted {len(rows)} 1D candles for {len(symbols)} symbols")
        except Exception as exc:
            logger.warning(f"daily candle upsert failed: {exc}")

    def _close_annotation(self, key: PositionKey) -> dict | None:
        """Mark most recent open annotation for (symbol, side) as closed.

        Aggregates PnL across every bybit_closed_pnl row emitted between
        annotation's opened_at and now. Bybit emits one closed_pnl row per
        partial reduce (each limit fill that shrinks the position), so a
        single-row lookup undercounts trades that were scaled out via
        multiple limit closes. Sum captures the full lifecycle.
        """
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, entry_price, size, opened_at,
                       trigger_condition, thesis_invalidation
                FROM bybit_trade_annotations
                WHERE symbol = %s AND side = %s AND status = 'open'
                ORDER BY opened_at DESC LIMIT 1
                """,
                (key.symbol, key.side),
            )
            annot = cur.fetchone()
            if not annot:
                return None

            missing: list[str] = []
            if not annot.get("trigger_condition"):
                missing.append("trigger_condition")
            if not annot.get("thesis_invalidation"):
                missing.append("thesis_invalidation")
            if missing:
                logger.warning(
                    f"bybit_watcher: journal_fields_missing on close "
                    f"annotation_id={annot['id']} symbol={key.symbol} side={key.side} "
                    f"missing={missing}"
                )

            # Sum every closed_pnl row for this symbol since the position opened.
            # Bybit's closed_pnl.side is the side of the closing order (opposite
            # to position side), so we filter by symbol + time window only.
            # Small clock-skew buffer so the first partial doesn't slip through.
            cur.execute(
                """
                SELECT
                    SUM(closed_pnl)                          AS total_pnl,
                    SUM(cum_entry_value)                     AS total_entry_value,
                    SUM(cum_exit_value)                      AS total_exit_value,
                    SUM(qty * avg_exit_price)
                        / NULLIF(SUM(qty), 0)                AS weighted_exit_price,
                    MAX(updated_time)                        AS last_updated,
                    COUNT(*)                                 AS rows_counted,
                    (array_agg(id ORDER BY updated_time DESC))[1] AS last_id
                FROM bybit_closed_pnl
                WHERE symbol = %s
                  AND updated_time >= %s - INTERVAL '1 minute'
                """,
                (key.symbol, annot["opened_at"]),
            )
            pnl_row = cur.fetchone()

            rows_counted = int(pnl_row["rows_counted"]) if pnl_row else 0
            pnl_usd = float(pnl_row["total_pnl"]) if rows_counted else None
            exit_price = float(pnl_row["weighted_exit_price"]) if rows_counted and pnl_row.get("weighted_exit_price") else None
            pnl_pct = None
            if rows_counted and pnl_row.get("total_entry_value"):
                pnl_pct = 100.0 * pnl_usd / float(pnl_row["total_entry_value"])
            last_id = pnl_row["last_id"] if rows_counted else None

            if rows_counted > 1:
                logger.info(
                    f"CLOSE_AGG {key.symbol} {key.side} partials={rows_counted} "
                    f"total_pnl={pnl_usd}"
                )

            cur.execute(
                """
                UPDATE bybit_trade_annotations
                SET closed_at = NOW(),
                    exit_price = %s,
                    pnl_usd = %s,
                    pnl_pct = %s,
                    closed_pnl_id = %s,
                    status = 'closed',
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id, entry_price, size, opened_at
                """,
                (exit_price, pnl_usd, pnl_pct, last_id, annot["id"]),
            )
            updated = cur.fetchone()
            conn.commit()
            return {
                "annotation_id": updated["id"],
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "exit_price": exit_price,
                "entry_price": float(updated["entry_price"]) if updated["entry_price"] else None,
                "partial_count": rows_counted,
            }

    def _fmt_auto_block(self, auto: dict) -> list[str]:
        """Shared auto-classifier summary block for alerts."""
        if not auto:
            return []
        setup = auto.get("auto_setup_type") or "?"
        grade = auto.get("auto_grade") or "?"
        conflu = auto.get("auto_confluences") or []
        detr = auto.get("auto_detractors") or []
        lines = [
            f"🧠 <b>{setup}</b> · grade <b>{grade}</b> · +{len(conflu)} / -{len(detr)}",
        ]
        if conflu:
            lines.append("✅ " + ", ".join(conflu[:6]) + ("…" if len(conflu) > 6 else ""))
        if detr:
            lines.append("❌ " + ", ".join(detr[:4]) + ("…" if len(detr) > 4 else ""))
        return lines

    def _fmt_open_alert(self, st: PositionState, annotation_id: int, ctx: dict, auto: dict) -> str:
        side_emoji = "🟢 LONG" if st.key.side == "Buy" else "🔴 SHORT"
        url = f"{self._dashboard_base}/annotate/{annotation_id}"
        lines = [
            f"<b>📥 TRADE OPEN — {st.key.symbol}</b>",
            f"{side_emoji} · size <code>{st.size}</code> · entry <code>{st.entry_price:.4f}</code> · lev <code>{st.leverage:g}x</code>",
            "",
        ]
        lines.extend(self._fmt_auto_block(auto))
        if auto:
            lines.append("")
        # Include selected context highlights
        htf = ctx.get("htf_bias") or {}
        if htf:
            align = htf.get("aligned_with_trade")
            flag = "✅" if align else ("⚠️" if align is False else "•")
            lines.append(f"{flag} HTF: 4H <b>{htf.get('bias_4h', '?')}</b> / 1H <b>{htf.get('bias_1h', '?')}</b>")
        funding = ctx.get("funding")
        if funding is not None:
            lines.append(f"• Funding: <code>{funding:+.4f}%</code>")
        oi = ctx.get("oi_delta_1h_pct")
        if oi is not None:
            lines.append(f"• OI 1h: <code>{oi:+.2f}%</code>")
        liq = ctx.get("nearest_liq_cluster") or {}
        if liq:
            lines.append(f"• Liq cluster: <b>{liq.get('side')}</b> @ <code>{liq.get('price')}</code> ({liq.get('distance_pct', 0):+.2f}%)")
        vp = ctx.get("volume_profile") or {}
        if vp:
            lines.append(f"• VP zone: <b>{vp.get('zone', '?')}</b> · POC <code>{vp.get('poc')}</code>")
        warn = ctx.get("warnings") or []
        if warn:
            lines.append("")
            lines.append("⚠️ " + " · ".join(warn))
        lines.append("")
        lines.append(f'<a href="{url}">📝 Anotar trade</a>')
        return "\n".join(lines)

    def _fmt_pending_alert(self, p: PendingOrder, pending_id: int, ctx: dict, auto: dict) -> str:
        side_emoji = "🟢 LONG" if p.side == "Buy" else "🔴 SHORT"
        type_tag = p.stop_order_type or p.order_type or "LIMIT"
        price_str = f"{p.price:.4f}" if p.price else (f"trig {p.trigger_price:.4f}" if p.trigger_price else "?")
        url = f"{self._dashboard_base}/pending/{pending_id}"
        lines = [
            f"<b>🟡 ORDER PLACED — {p.symbol}</b>",
            f"{side_emoji} · <code>{type_tag}</code> · qty <code>{p.qty}</code> @ <code>{price_str}</code>",
            "",
        ]
        lines.extend(self._fmt_auto_block(auto))
        if auto:
            lines.append("")
        htf = ctx.get("htf_bias") or {}
        if htf:
            align = htf.get("aligned_with_trade")
            flag = "✅" if align else ("⚠️" if align is False else "•")
            lines.append(f"{flag} HTF: 4H <b>{htf.get('bias_4h', '?')}</b> / 1H <b>{htf.get('bias_1h', '?')}</b>")
        funding = ctx.get("funding")
        if funding is not None:
            lines.append(f"• Funding: <code>{funding:+.4f}%</code>")
        warn = ctx.get("warnings") or []
        if warn:
            lines.append("")
            lines.append("⚠️ " + " · ".join(warn))
        lines.append("")
        lines.append(f'<a href="{url}">📝 Anotar thesis ANTES del fill</a>')
        return "\n".join(lines)

    def _fmt_pending_terminal(self, p: PendingOrder, terminal: str) -> str:
        emoji = "✅" if terminal == "filled" else "⚪"
        verb = "FILLED" if terminal == "filled" else "CANCELLED"
        side_emoji = "🟢" if p.side == "Buy" else "🔴"
        return (
            f"<b>{emoji} {verb} — {p.symbol}</b>\n"
            f"{side_emoji} {p.side} · qty <code>{p.qty}</code> @ <code>{p.price:.4f}</code>"
        )

    def _fmt_close_alert(self, st: PositionState, closure: dict) -> str:
        pnl = closure.get("pnl_usd") or 0
        pct = closure.get("pnl_pct")
        emoji = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
        url = f"{self._dashboard_base}/annotate/{closure['annotation_id']}"
        pct_str = f" ({pct:+.2f}%)" if pct is not None else ""
        return (
            f"<b>{emoji} TRADE CLOSED — {st.key.symbol} {st.key.side}</b>\n"
            f"PnL: <code>${pnl:+.2f}</code>{pct_str}\n"
            f"Entry <code>{closure.get('entry_price')}</code> → Exit <code>{closure.get('exit_price')}</code>\n\n"
            f'<a href="{url}">📝 Post-mortem</a>'
        )

    async def _emit_pending_diff(
        self,
        prev: dict[str, PendingOrder],
        curr: dict[str, PendingOrder],
        opened_pos_keys: list[PositionKey],
    ) -> None:
        new_ids = [oid for oid in curr if oid not in prev]
        gone_ids = [oid for oid in prev if oid not in curr]

        # For orders that disappeared, determine filled vs cancelled.
        # Heuristic: if a position opened in the SAME tick for (symbol, side), treat as filled.
        # Otherwise query Bybit order history for final state.
        pos_sides = {(k.symbol, k.side) for k in opened_pos_keys}

        for oid in new_ids:
            p = curr[oid]
            ctx = self._context_snapshot(PositionKey(symbol=p.symbol, side=p.side))
            auto = self._classify(ctx)
            pid = self._upsert_pending(p, ctx, auto)
            await self._send_telegram(self._fmt_pending_alert(p, pid, ctx, auto))
            logger.info(f"PENDING_NEW {p.symbol} {p.side} qty={p.qty} price={p.price} id={pid} auto={auto.get('auto_setup_type')}/{auto.get('auto_grade')}")

        for oid in gone_ids:
            p = prev[oid]
            if (p.symbol, p.side) in pos_sides:
                terminal = "filled"
            else:
                terminal = self._classify_gone_order(oid)
            self._resolve_pending(oid, terminal)
            await self._send_telegram(self._fmt_pending_terminal(p, terminal))
            logger.info(f"PENDING_{terminal.upper()} {p.symbol} {p.side} id={oid}")

    def _classify_gone_order(self, order_id: str) -> str:
        """Query Bybit order history to determine why order disappeared."""
        try:
            resp = self.client.get_order_history(category="linear", orderId=order_id, limit=1)
            rows = (resp.get("result") or {}).get("list", []) or []
            if not rows:
                return "cancelled"
            status = (rows[0].get("orderStatus") or "").lower()
            if status in ("filled", "partiallyfilled"):
                return "filled"
            return "cancelled"
        except Exception as exc:
            logger.warning(f"order_history lookup failed for {order_id}: {exc}")
            return "cancelled"

    async def _emit_diff(self, prev: dict[PositionKey, PositionState], curr: dict[PositionKey, PositionState]) -> None:
        opened = [k for k in curr if k not in prev]
        closed = [k for k in prev if k not in curr]
        modified = [k for k in curr if k in prev and abs(curr[k].size - prev[k].size) > 1e-9]

        for k in opened:
            st = curr[k]
            ctx = self._context_snapshot(k)
            auto = self._classify(ctx)
            annot_id = self._insert_annotation(st, ctx, auto)
            # Try to carry forward thesis from matching recently-filled pending order
            carried = self._link_annotation_from_pending(
                k.symbol, k.side, annot_id,
                size=st.size, entry_price=st.entry_price,
            )
            if carried:
                logger.info(f"OPEN {k.symbol} {k.side} carried thesis from pending order {carried.get('order_id')}")
            await self._send_telegram(self._fmt_open_alert(st, annot_id, ctx, auto))
            logger.info(f"OPEN {k.symbol} {k.side} size={st.size} entry={st.entry_price} annot_id={annot_id} auto={auto.get('auto_setup_type')}/{auto.get('auto_grade')}")

        for k in closed:
            prev_st = prev[k]
            # sync closed_pnl so we can attach pnl
            try:
                from data_service.bybit_sync import BybitSync
                BybitSync().sync_closed_pnl(category="linear", days=1)
            except Exception as exc:
                logger.warning(f"closed_pnl sync failed: {exc}")
            closure = self._close_annotation(k)
            if closure:
                await self._send_telegram(self._fmt_close_alert(prev_st, closure))
            logger.info(f"CLOSED {k.symbol} {k.side} closure={closure}")

        for k in modified:
            delta = curr[k].size - prev[k].size
            direction = "ADD" if delta > 0 else "REDUCE"
            logger.info(f"{direction} {k.symbol} {k.side} delta={delta:+.4f} new_size={curr[k].size}")

        # SL attach/trail sync — size-unchanged stopLoss edits are missed by the
        # `modified` (size-delta) branch, so check every still-open position.
        for k in curr:
            if k in prev and curr[k].raw.get("stopLoss") != prev[k].raw.get("stopLoss"):
                self._refresh_sl(curr[k])

    async def _enforce_journal_deadline(self, curr_pending: dict[str, PendingOrder]) -> None:
        """Rule 6 enforcement — cancel pending limit orders that have no thesis_pre filled past deadline.

        Skips Market orders (whitelist) and orders already past terminal state.
        Stamps `enforcement_cancelled_at` on success so we don't double-fire.
        Bybit cancel triggers normal _emit_pending_diff flow on next tick.
        """
        if not settings.BYBIT_JOURNAL_ENFORCEMENT_ENABLED:
            return
        deadline_sec = settings.BYBIT_JOURNAL_ENFORCEMENT_DEADLINE_SEC
        whitelist = set(settings.BYBIT_JOURNAL_ENFORCEMENT_WHITELIST_ORDER_TYPES)
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT order_id, symbol, side, order_type, placed_at
                FROM bybit_pending_orders
                WHERE status = 'pending'
                  AND thesis_pre IS NULL
                  AND enforcement_cancelled_at IS NULL
                  AND placed_at <= NOW() - (%s || ' seconds')::INTERVAL
                """,
                (deadline_sec,),
            )
            candidates = [dict(r) for r in cur.fetchall()]
        for c in candidates:
            if c["order_type"] in whitelist:
                continue
            if c["order_id"] not in curr_pending:
                # Order already gone from Bybit — let _emit_pending_diff handle as filled/cancelled.
                continue
            try:
                self.client.cancel_order(category="linear", symbol=c["symbol"], orderId=c["order_id"])
            except Exception as exc:
                logger.error(f"enforcement cancel failed for {c['order_id']}: {exc}")
                await self._send_telegram(
                    f"⚠️ Enforcement cancel FAILED for {c['symbol']} {c['side']}: {exc}"
                )
                continue
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE bybit_pending_orders SET enforcement_cancelled_at = NOW(), updated_at = NOW() WHERE order_id = %s",
                    (c["order_id"],),
                )
                conn.commit()
            mins = deadline_sec // 60
            await self._send_telegram(
                f"<b>❌ ORDEN AUTO-CANCELADA</b>\n"
                f"{c['symbol']} {c['side']} — sin <code>thesis_pre</code> tras {mins} min.\n"
                f"Rule 6 enforcement. Llena el journal antes de la próxima orden."
            )
            logger.warning(
                f"ENFORCEMENT_CANCEL {c['symbol']} {c['side']} order_id={c['order_id']} "
                f"age_sec={(datetime.now(tz=timezone.utc) - c['placed_at']).total_seconds():.0f}"
            )

    async def tick(self) -> None:
        try:
            curr_pos = self._fetch_positions()
        except Exception as exc:
            logger.error(f"fetch_positions failed: {exc}")
            return
        try:
            curr_pending = self._fetch_pending()
        except Exception as exc:
            logger.error(f"fetch_pending failed: {exc}")
            curr_pending = {}

        # Rule 6 enforcement runs BEFORE diff so cancel resolves naturally on next tick
        try:
            await self._enforce_journal_deadline(curr_pending)
        except Exception as exc:
            logger.error(f"_enforce_journal_deadline failed: {exc}")

        # Emit position diff first (so pending diff can detect filled orders)
        opened_pos = [k for k in curr_pos if k not in self._last_state]
        await self._emit_pending_diff(self._last_pending, curr_pending, opened_pos)
        await self._emit_diff(self._last_state, curr_pos)
        self._last_state = curr_pos
        self._last_pending = curr_pending

    async def _periodic_sync_loop(self) -> None:
        """Pull bybit_executions + bybit_closed_pnl on a fixed interval.

        Removes the manual `python scripts/sync_bybit.py` dependency — without
        this loop, both tables drift whenever a close event is missed (idle
        watcher, restart, transient API error). Idempotent inserts make the
        overlap window safe to replay.
        """
        if not settings.BYBIT_PERIODIC_SYNC_ENABLED:
            logger.info("bybit_watcher: periodic sync disabled")
            return
        interval = max(60, int(settings.BYBIT_PERIODIC_SYNC_SEC))
        days = max(1, int(settings.BYBIT_PERIODIC_SYNC_DAYS))
        logger.info(f"bybit_watcher: periodic sync every {interval}s (last {days}d)")
        from data_service.bybit_sync import BybitSync
        sync = BybitSync()
        while True:
            await asyncio.sleep(interval)
            try:
                exec_n = sync.sync_executions(category="linear", days=days)
                pnl_n = sync.sync_closed_pnl(category="linear", days=days)
                logger.info(f"periodic_sync: execs={exec_n} closed_pnl={pnl_n}")
            except Exception as exc:
                logger.warning(f"periodic_sync failed: {exc}")

    async def run_forever(self, interval: int = POLL_INTERVAL_SEC) -> None:
        logger.info(f"bybit_watcher: started (interval={interval}s)")
        asyncio.create_task(self._periodic_sync_loop())
        self._backfill_daily_candles()
        refresh_every = max(1, 3600 // max(1, interval))
        poll_count = 0
        while True:
            await self.tick()
            poll_count += 1
            if poll_count % refresh_every == 0:
                self._backfill_daily_candles()
            await asyncio.sleep(interval)


async def _once() -> None:
    w = BybitWatcher()
    await w.tick()
    # run second tick to emit diff
    await asyncio.sleep(2)
    await w.tick()


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        asyncio.run(_once())
    else:
        interval = int(os.getenv("BYBIT_WATCH_INTERVAL", POLL_INTERVAL_SEC))
        asyncio.run(BybitWatcher().run_forever(interval=interval))
