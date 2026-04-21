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
from psycopg2.extras import Json, RealDictCursor
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

    def _link_annotation_from_pending(self, symbol: str, side: str, annotation_id: int) -> dict | None:
        """When a position opens, try to carry forward the most recent pending order's thesis.
        Matches by (symbol, side) filled within last 5 min.
        """
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
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
        sql = """
        INSERT INTO bybit_trade_annotations (
            symbol, side, opened_at, entry_price, size, leverage,
            notional_value, context_snapshot,
            auto_setup_type, auto_confluences, auto_detractors,
            auto_grade, auto_classifier_version,
            status
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
        ON CONFLICT (symbol, side, opened_at) DO UPDATE
            SET context_snapshot = EXCLUDED.context_snapshot,
                auto_setup_type = EXCLUDED.auto_setup_type,
                auto_confluences = EXCLUDED.auto_confluences,
                auto_detractors = EXCLUDED.auto_detractors,
                auto_grade = EXCLUDED.auto_grade,
                auto_classifier_version = EXCLUDED.auto_classifier_version
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
            ))
            row = cur.fetchone()
            conn.commit()
            return row[0]

    def _close_annotation(self, key: PositionKey) -> dict | None:
        """Mark most recent open annotation for (symbol, side) as closed.
        Pulls PnL from latest bybit_closed_pnl row.
        """
        with self._conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, entry_price, size FROM bybit_trade_annotations
                WHERE symbol = %s AND side = %s AND status = 'open'
                ORDER BY opened_at DESC LIMIT 1
                """,
                (key.symbol, key.side),
            )
            annot = cur.fetchone()
            if not annot:
                return None

            cur.execute(
                """
                SELECT id, closed_pnl, avg_exit_price, updated_time, cum_entry_value, cum_exit_value
                FROM bybit_closed_pnl
                WHERE symbol = %s AND updated_time >= NOW() - INTERVAL '5 minutes'
                ORDER BY updated_time DESC LIMIT 1
                """,
                (key.symbol,),
            )
            pnl_row = cur.fetchone()

            pnl_usd = float(pnl_row["closed_pnl"]) if pnl_row else None
            exit_price = float(pnl_row["avg_exit_price"]) if pnl_row else None
            pnl_pct = None
            if pnl_row and pnl_row.get("cum_entry_value"):
                pnl_pct = 100.0 * pnl_usd / float(pnl_row["cum_entry_value"])

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
                (exit_price, pnl_usd, pnl_pct,
                 pnl_row["id"] if pnl_row else None, annot["id"]),
            )
            updated = cur.fetchone()
            conn.commit()
            return {
                "annotation_id": updated["id"],
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "exit_price": exit_price,
                "entry_price": float(updated["entry_price"]) if updated["entry_price"] else None,
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
        side_emoji = "🟢 LONG" if st.side == "Buy" else "🔴 SHORT"
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
            carried = self._link_annotation_from_pending(k.symbol, k.side, annot_id)
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

        # Bootstrap: first tick, snapshot without alerts
        bootstrap = not self._last_state and not self._last_pending
        if bootstrap and (curr_pos or curr_pending):
            logger.info(
                f"bootstrap: {len(curr_pos)} position(s) + {len(curr_pending)} pending order(s), "
                "snapshotting without alerts"
            )
            # Insert existing pending into DB without alerting
            for p in curr_pending.values():
                ctx = self._context_snapshot(PositionKey(symbol=p.symbol, side=p.side))
                auto = self._classify(ctx)
                self._upsert_pending(p, ctx, auto)
            self._last_state = curr_pos
            self._last_pending = curr_pending
            return

        # Emit position diff first (so pending diff can detect filled orders)
        opened_pos = [k for k in curr_pos if k not in self._last_state]
        await self._emit_pending_diff(self._last_pending, curr_pending, opened_pos)
        await self._emit_diff(self._last_state, curr_pos)
        self._last_state = curr_pos
        self._last_pending = curr_pending

    async def run_forever(self, interval: int = POLL_INTERVAL_SEC) -> None:
        logger.info(f"bybit_watcher: started (interval={interval}s)")
        while True:
            await self.tick()
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
