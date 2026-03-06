"""
Position monitor — background async loop that manages position lifecycle.

Polls order status every ORDER_POLL_INTERVAL seconds and transitions
positions through the state machine:

    pending_entry ──[fill]──────> active         (place SL + TP1/TP2/TP3)
    pending_entry ──[15min]─────> closed         (cancel entry)

    active ──[TP1 fills]──> tp1_hit              (SL → breakeven)
    active ──[SL fills]───> closed               (cancel all TPs)
    active ──[12h]────────> closed               (market close all)

    tp1_hit ──[TP2 fills]──> tp2_hit             (SL → TP1 level)
    tp1_hit ──[SL fills]───> closed              (cancel remaining TPs)

    tp2_hit ──[TP3 fills]──> closed              (fully done)
    tp2_hit ──[SL fills]───> closed              (cancel TP3)

Critical safety:
- Entry fill + SL placement fails → EMERGENCY market close
- SL adjustment: place NEW SL first, then cancel OLD SL (zero gap)
- Slippage logging on every fill
"""

import asyncio
import json
import time
from typing import Optional

from config.settings import settings, QUICK_SETUP_TYPES
from shared.logger import setup_logger
from execution_service.models import ManagedPosition
from execution_service.executor import OrderExecutor

logger = setup_logger("execution_service")


class PositionMonitor:
    """Background loop managing open positions through their lifecycle."""

    def __init__(self, executor: OrderExecutor, risk_service,
                 data_store=None, notifier=None) -> None:
        self._executor = executor
        self._risk = risk_service
        self._data_store = data_store  # DataService for DB persistence
        self._notifier = notifier      # TelegramNotifier (optional)
        self._positions: dict[str, ManagedPosition] = {}  # keyed by pair
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def positions(self) -> dict[str, ManagedPosition]:
        return self._positions

    # ================================================================
    # Lifecycle
    # ================================================================

    def start(self) -> None:
        """Start the monitor loop as a background task."""
        if self._running:
            return
        self._running = True
        # Clear stale positions cache from previous run — in-memory is
        # authoritative, Redis is just a dashboard mirror.
        self._update_positions_cache()
        self._task = asyncio.create_task(self._poll_loop(), name="position_monitor")
        logger.info("Position monitor started")

    async def stop(self) -> None:
        """Stop the monitor loop. Cancel unfilled entries, leave filled positions
        (SL/TP live on exchange and survive bot shutdown).
        """
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Cancel unfilled entry orders only
        for pair, pos in list(self._positions.items()):
            if pos.phase == "pending_entry" and pos.entry_order_id:
                await self._executor.cancel_order(pos.entry_order_id, pair)
                self._close_position(pos, "cancelled")

        logger.info("Position monitor stopped")

    def register(self, pos: ManagedPosition) -> None:
        """Register a new position for monitoring."""
        self._positions[pos.pair] = pos
        self._update_positions_cache()
        logger.info(f"Position registered: {pos.pair} {pos.direction} phase={pos.phase}")

    # ================================================================
    # Main poll loop
    # ================================================================

    async def _poll_loop(self) -> None:
        """Poll order status at regular intervals."""
        while self._running:
            try:
                await self._check_all_positions()
            except Exception as e:
                logger.error(f"Monitor poll error: {e}")
            await asyncio.sleep(settings.ORDER_POLL_INTERVAL)

    async def _check_all_positions(self) -> None:
        """Check every managed position and advance state machine."""
        for pair in list(self._positions.keys()):
            pos = self._positions.get(pair)
            if pos is None or pos.phase == "closed":
                continue

            if pos.phase == "pending_entry":
                await self._check_pending_entry(pos)
            elif pos.phase in ("active", "tp1_hit", "tp2_hit"):
                await self._check_active_position(pos)
            elif pos.phase == "emergency_pending":
                await self._retry_emergency_close(pos)

    # ================================================================
    # State: pending_entry
    # ================================================================

    async def _check_pending_entry(self, pos: ManagedPosition) -> None:
        """Check if entry order filled or timed out."""
        now = int(time.time())

        # Timeout check
        if now - pos.created_at >= settings.ENTRY_TIMEOUT_SECONDS:
            logger.info(f"Entry timeout: {pos.pair} after {settings.ENTRY_TIMEOUT_SECONDS}s")
            if pos.entry_order_id:
                await self._executor.cancel_order(pos.entry_order_id, pos.pair)
            self._close_position(pos, "cancelled")
            return

        # Check entry order status
        if not pos.entry_order_id:
            return
        order = await self._executor.fetch_order(pos.entry_order_id, pos.pair)
        if order is None:
            return

        status = order.get("status", "")
        filled = float(order.get("filled", 0))

        if status == "closed" and filled > 0:
            # Fully filled
            actual_price = float(order.get("average", 0) or order.get("price", 0))
            self._log_slippage(pos, actual_price)
            pos.actual_entry_price = actual_price
            pos.filled_size = filled
            pos.filled_at = int(time.time())
            await self._on_entry_filled(pos)

        elif status == "canceled" or status == "cancelled":
            # Check if partially filled
            if filled > 0:
                actual_price = float(order.get("average", 0) or order.get("price", 0))
                self._log_slippage(pos, actual_price)
                pos.actual_entry_price = actual_price
                pos.filled_size = filled
                pos.filled_at = int(time.time())
                await self._on_entry_filled(pos)
            else:
                self._close_position(pos, "cancelled")

    async def _on_entry_filled(self, pos: ManagedPosition) -> None:
        """Entry filled — place SL + TPs. If SL fails → emergency close."""
        close_side = "sell" if pos.direction == "long" else "buy"

        # In sandbox mode, remap SL/TP relative to actual fill price
        # (sandbox prices differ from real market; preserve R:R ratios)
        if settings.OKX_SANDBOX and pos.actual_entry_price and pos.entry_price:
            self._remap_sandbox_prices(pos)

        # Place stop-loss FIRST (most critical)
        sl_order = await self._executor.place_stop_market(
            pos.pair, close_side, pos.filled_size, pos.sl_price
        )

        if sl_order is None:
            logger.error(f"SL placement FAILED after entry fill — EMERGENCY CLOSE: {pos.pair}")
            # Telegram: emergency close
            if self._notifier is not None:
                self._safe_notify(
                    self._notifier.notify_emergency(pos, "SL placement failed — market closed")
                )
            result = await self._executor.close_position_market(
                pos.pair, close_side, pos.filled_size
            )
            if result is None:
                logger.critical(f"EMERGENCY CLOSE FAILED: {pos.pair} — will retry on next poll cycle")
                pos.phase = "emergency_pending"
                pos.emergency_retries = 1
                return
            self._close_position(pos, "emergency")
            return

        pos.sl_order_id = sl_order.get("id")

        # Place TPs (scaled by TP close percentages)
        tp1_size = round(pos.filled_size * settings.TP1_CLOSE_PCT, 8)
        tp2_size = round(pos.filled_size * settings.TP2_CLOSE_PCT, 8)
        tp3_size = round(pos.filled_size * settings.TP3_CLOSE_PCT, 8)

        tp1 = await self._executor.place_take_profit(
            pos.pair, close_side, tp1_size, pos.tp1_price
        )
        tp2 = await self._executor.place_take_profit(
            pos.pair, close_side, tp2_size, pos.tp2_price
        )
        tp3 = await self._executor.place_take_profit(
            pos.pair, close_side, tp3_size, pos.tp3_price
        )

        pos.tp1_order_id = tp1.get("id") if tp1 else None
        pos.tp2_order_id = tp2.get("id") if tp2 else None
        pos.tp3_order_id = tp3.get("id") if tp3 else None

        if not tp1 or not tp2 or not tp3:
            logger.error(f"TP order placement failed: {pos.pair} "
                         f"tp1={'ok' if tp1 else 'FAIL'} "
                         f"tp2={'ok' if tp2 else 'FAIL'} "
                         f"tp3={'ok' if tp3 else 'FAIL'} "
                         f"— EMERGENCY CLOSE (missing TP = no SL adjustment)")

            # Cancel all successfully placed TPs and SL
            for order_id in (pos.tp1_order_id, pos.tp2_order_id, pos.tp3_order_id):
                if order_id:
                    await self._executor.cancel_order(order_id, pos.pair)
            if pos.sl_order_id:
                await self._executor.cancel_order(pos.sl_order_id, pos.pair)

            # Emergency market close
            if self._notifier is not None:
                self._safe_notify(self._notifier.notify_emergency(
                    pos, "TP placement failed — emergency market close"
                ))
            close_side = "sell" if pos.direction == "long" else "buy"
            result = await self._executor.close_position_market(
                pos.pair, close_side, pos.filled_size
            )
            if result is None:
                logger.critical(f"EMERGENCY CLOSE FAILED after TP failure: {pos.pair}")
                pos.phase = "emergency_pending"
                pos.emergency_retries = 1
                return
            self._close_position(pos, "emergency")
            return

        pos.phase = "active"
        self._update_positions_cache()
        logger.info(f"Position ACTIVE: {pos.pair} {pos.direction} "
                     f"entry={pos.actual_entry_price} size={pos.filled_size:.6f}")

        # Telegram: trade opened
        if self._notifier is not None:
            self._safe_notify(self._notifier.notify_trade_opened(pos))

        # Persist trade to PostgreSQL
        self._persist_trade_open(pos)

    # ================================================================
    # State: active / tp1_hit / tp2_hit
    # ================================================================

    async def _check_active_position(self, pos: ManagedPosition) -> None:
        """Check SL and TP order statuses, advance state machine."""
        now = int(time.time())

        # Max duration timeout (4h for quick setups, 12h for swing)
        max_duration = (settings.MAX_TRADE_DURATION_QUICK
                        if pos.setup_type in QUICK_SETUP_TYPES
                        else settings.MAX_TRADE_DURATION_SECONDS)
        created = pos.filled_at or pos.created_at
        if now - created >= max_duration:
            logger.info(f"Trade duration timeout: {pos.pair} after "
                        f"{max_duration}s")
            await self._close_all_orders_and_market_close(pos)
            return

        # Check SL
        if pos.sl_order_id:
            sl_status = await self._executor.fetch_order(pos.sl_order_id, pos.pair)
            if sl_status and sl_status.get("status") == "closed":
                logger.info(f"SL hit: {pos.pair} {pos.direction}")
                await self._cancel_remaining_tps(pos)
                self._calculate_pnl(pos, float(sl_status.get("average", 0) or pos.sl_price))
                self._close_position(pos, "sl")
                return

        # Check TPs based on phase
        if pos.phase == "active" and pos.tp1_order_id:
            tp1_status = await self._executor.fetch_order(pos.tp1_order_id, pos.pair)
            if tp1_status and tp1_status.get("status") == "closed":
                logger.info(f"TP1 hit: {pos.pair} — moving SL to breakeven")
                self._accumulate_realized_pnl(pos, pos.tp1_price, settings.TP1_CLOSE_PCT)
                await self._adjust_sl(pos, pos.actual_entry_price)
                pos.phase = "tp1_hit"
                return

        if pos.phase == "tp1_hit" and pos.tp2_order_id:
            tp2_status = await self._executor.fetch_order(pos.tp2_order_id, pos.pair)
            if tp2_status and tp2_status.get("status") == "closed":
                logger.info(f"TP2 hit: {pos.pair} — moving SL to TP1 level")
                self._accumulate_realized_pnl(pos, pos.tp2_price, settings.TP2_CLOSE_PCT)
                await self._adjust_sl(pos, pos.tp1_price)
                pos.phase = "tp2_hit"
                return

        if pos.phase == "tp2_hit" and pos.tp3_order_id:
            tp3_status = await self._executor.fetch_order(pos.tp3_order_id, pos.pair)
            if tp3_status and tp3_status.get("status") == "closed":
                logger.info(f"TP3 hit: {pos.pair} — position fully closed")
                # Cancel SL since position is fully closed
                if pos.sl_order_id:
                    await self._executor.cancel_order(pos.sl_order_id, pos.pair)
                self._accumulate_realized_pnl(pos, pos.tp3_price, settings.TP3_CLOSE_PCT)
                self._calculate_pnl(pos, pos.tp3_price)
                self._close_position(pos, "tp3")
                return

    # ================================================================
    # State: emergency_pending (retry failed emergency close)
    # ================================================================

    async def _retry_emergency_close(self, pos: ManagedPosition) -> None:
        """Retry emergency market close (max 3 attempts)."""
        close_side = "sell" if pos.direction == "long" else "buy"
        result = await self._executor.close_position_market(
            pos.pair, close_side, pos.filled_size
        )
        if result is not None:
            logger.info(f"Emergency close succeeded on retry {pos.emergency_retries}: {pos.pair}")
            self._close_position(pos, "emergency")
            return

        pos.emergency_retries += 1
        if pos.emergency_retries >= 3:
            logger.critical(
                f"EMERGENCY CLOSE FAILED after 3 retries: {pos.pair} — "
                f"requires manual intervention"
            )
            pos.phase = "emergency_failed"
        else:
            logger.error(
                f"Emergency close retry {pos.emergency_retries}/3 failed: {pos.pair}"
            )

    # ================================================================
    # SL adjustment — new SL first, then cancel old (zero gap)
    # ================================================================

    async def _adjust_sl(self, pos: ManagedPosition, new_price: float) -> None:
        """Move SL to new level. Place new BEFORE cancelling old.

        Race window: Between placing the new SL and cancelling the old one,
        both SL orders exist simultaneously. If price hits both, we could get
        a double close. Mitigation: both SLs use reduceOnly, so the second
        would close zero size (no net position to reduce). A fully atomic
        SL amendment via OKX amend-order API would eliminate this race entirely.
        TODO: migrate to OKX amend-order API for atomic SL updates.
        """
        close_side = "sell" if pos.direction == "long" else "buy"

        # Remaining size after partial closes
        remaining = self._remaining_size(pos)

        # Place new SL first
        new_sl = await self._executor.place_stop_market(
            pos.pair, close_side, remaining, new_price
        )

        if new_sl is None:
            logger.error(f"New SL placement failed — keeping old SL: {pos.pair}")
            return

        # Cancel old SL
        old_sl_id = pos.sl_order_id
        if old_sl_id:
            await self._executor.cancel_order(old_sl_id, pos.pair)

        pos.sl_order_id = new_sl.get("id")
        logger.info(f"SL adjusted: {pos.pair} new_price={new_price:.2f}")

    # ================================================================
    # Helpers
    # ================================================================

    def _accumulate_realized_pnl(
        self, pos: ManagedPosition, fill_price: float, tranche_pct: float
    ) -> None:
        """Accumulate realized PnL for a filled TP tranche."""
        if not pos.actual_entry_price:
            return
        tranche_size = pos.filled_size * tranche_pct
        if pos.direction == "long":
            pnl = (fill_price - pos.actual_entry_price) * tranche_size
        else:
            pnl = (pos.actual_entry_price - fill_price) * tranche_size
        pos.realized_pnl_usd += pnl

    @staticmethod
    def _remap_sandbox_prices(pos: ManagedPosition) -> None:
        """Remap SL/TP prices relative to actual fill price in sandbox.

        Sandbox prices differ from real market. This preserves the percentage
        distance of SL/TP from the intended entry so the trade lifecycle
        (SL trigger, TP fills, SL adjustments) works correctly in demo mode.
        """
        intended = pos.entry_price
        actual = pos.actual_entry_price
        if not intended or not actual or intended == 0:
            return

        ratio = actual / intended

        old_sl = pos.sl_price
        pos.sl_price = round(pos.sl_price * ratio, 2)
        pos.tp1_price = round(pos.tp1_price * ratio, 2)
        pos.tp2_price = round(pos.tp2_price * ratio, 2)
        pos.tp3_price = round(pos.tp3_price * ratio, 2)

        logger.info(
            f"Sandbox price remap: {pos.pair} ratio={ratio:.4f} "
            f"sl={old_sl:.2f}->{pos.sl_price:.2f} "
            f"tp1={pos.tp1_price:.2f} tp2={pos.tp2_price:.2f} tp3={pos.tp3_price:.2f}"
        )

    def _remaining_size(self, pos: ManagedPosition) -> float:
        """Calculate remaining position size based on phase."""
        if pos.phase == "active":
            return pos.filled_size
        elif pos.phase == "tp1_hit":
            return round(pos.filled_size * (1 - settings.TP1_CLOSE_PCT), 8)
        elif pos.phase == "tp2_hit":
            return round(pos.filled_size * settings.TP3_CLOSE_PCT, 8)
        return 0.0

    async def _cancel_remaining_tps(self, pos: ManagedPosition) -> None:
        """Cancel all TP orders that haven't filled yet."""
        for tp_attr in ("tp1_order_id", "tp2_order_id", "tp3_order_id"):
            order_id = getattr(pos, tp_attr)
            if order_id:
                await self._executor.cancel_order(order_id, pos.pair)

    async def _close_all_orders_and_market_close(self, pos: ManagedPosition) -> None:
        """Cancel all orders and market close the position (timeout)."""
        # Cancel SL and TPs
        if pos.sl_order_id:
            await self._executor.cancel_order(pos.sl_order_id, pos.pair)
        await self._cancel_remaining_tps(pos)

        # Market close remaining
        close_side = "sell" if pos.direction == "long" else "buy"
        remaining = self._remaining_size(pos)
        if remaining > 0:
            await self._executor.close_position_market(pos.pair, close_side, remaining)

        self._close_position(pos, "timeout")

    def _safe_notify(self, coro) -> None:
        """Fire-and-forget notification with error logging."""
        task = asyncio.create_task(coro)
        task.add_done_callback(self._notify_task_done)

    @staticmethod
    def _notify_task_done(task: asyncio.Task) -> None:
        """Log exceptions from notification tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Notification task failed: {exc}")

    def _log_slippage(self, pos: ManagedPosition, actual_price: float) -> None:
        """Log expected vs actual fill price."""
        expected = pos.entry_price
        if expected > 0:
            slippage_pct = abs(actual_price - expected) / expected * 100
            logger.info(
                f"Slippage: {pos.pair} expected={expected:.2f} "
                f"actual={actual_price:.2f} diff={slippage_pct:.4f}%"
            )

    def _calculate_pnl(self, pos: ManagedPosition, exit_price: float) -> None:
        """Calculate blended PnL across realized TP tranches + unrealized remainder.

        Combines accumulated realized PnL from TP fills with unrealized PnL
        on the remaining position size at exit_price.
        """
        if not pos.actual_entry_price or pos.actual_entry_price == 0:
            return

        remaining = self._remaining_size(pos)

        # Unrealized PnL on remaining size at exit_price
        if pos.direction == "long":
            unrealized_usd = (exit_price - pos.actual_entry_price) * remaining
        else:
            unrealized_usd = (pos.actual_entry_price - exit_price) * remaining

        # Blended PnL: (realized from TPs + unrealized remainder) / notional
        total_pnl_usd = pos.realized_pnl_usd + unrealized_usd
        notional = pos.actual_entry_price * pos.filled_size
        if notional > 0:
            pos.pnl_pct = total_pnl_usd / notional
        else:
            pos.pnl_pct = 0.0

    def _close_position(self, pos: ManagedPosition, reason: str) -> None:
        """Transition position to closed and notify Risk Service."""
        pos.phase = "closed"
        pos.close_reason = reason
        pos.closed_at = int(time.time())

        logger.info(
            f"Position CLOSED: {pos.pair} {pos.direction} reason={reason} "
            f"pnl={pos.pnl_pct*100:.2f}%"
        )

        # Telegram: trade closed (skip cancelled entries — no real trade)
        if self._notifier is not None and reason != "cancelled":
            self._safe_notify(self._notifier.notify_trade_closed(pos))

        # Persist trade close to PostgreSQL
        self._persist_trade_close(pos)

        # Update Redis positions cache
        self._update_positions_cache()

        # Notify Risk Service (skip cancelled entries — not a real trade)
        if self._risk is not None and reason != "cancelled":
            self._risk.on_trade_closed(
                pos.pair, pos.direction, pos.pnl_pct, pos.closed_at
            )

        # Remove from tracking
        self._positions.pop(pos.pair, None)

    # ================================================================
    # DB persistence helpers
    # ================================================================

    def _persist_trade_open(self, pos: ManagedPosition) -> None:
        """Insert trade into PostgreSQL on entry fill.
        Slippage is persisted via actual_entry (expected vs actual fill price).
        """
        if self._data_store is None or self._data_store.postgres is None:
            return
        try:
            trade_id = self._data_store.postgres.insert_trade(
                pair=pos.pair,
                direction=pos.direction,
                setup_type=pos.setup_type,
                entry_price=pos.entry_price,
                sl_price=pos.sl_price,
                tp1_price=pos.tp1_price,
                tp2_price=pos.tp2_price,
                tp3_price=pos.tp3_price,
                position_size=pos.filled_size,
                ai_confidence=pos.ai_confidence,
                actual_entry=pos.actual_entry_price,
            )
            pos.db_trade_id = trade_id
        except Exception as e:
            logger.error(f"Failed to persist trade open: {pos.pair} {e}")

    def _persist_trade_close(self, pos: ManagedPosition) -> None:
        """Update trade in PostgreSQL on position close."""
        if self._data_store is None or self._data_store.postgres is None:
            return
        trade_id = getattr(pos, "db_trade_id", None)
        if trade_id is None:
            return
        try:
            # Calculate USD PnL
            pnl_usd = None
            if pos.actual_entry_price and pos.filled_size:
                pnl_usd = pos.actual_entry_price * pos.filled_size * pos.pnl_pct

            self._data_store.postgres.update_trade(
                trade_id=trade_id,
                actual_exit=None,  # We don't track exact exit price
                exit_reason=pos.close_reason,
                pnl_usd=pnl_usd,
                pnl_pct=pos.pnl_pct,
                status="closed",
            )
        except Exception as e:
            logger.error(f"Failed to persist trade close: {pos.pair} {e}")

    def _update_positions_cache(self) -> None:
        """Write current open positions to Redis for dashboard consumption."""
        if self._data_store is None:
            return
        try:
            positions_data = []
            for pair, pos in self._positions.items():
                if pos.phase == "closed":
                    continue
                positions_data.append({
                    "pair": pos.pair,
                    "direction": pos.direction,
                    "setup_type": pos.setup_type,
                    "phase": pos.phase,
                    "entry_price": pos.entry_price,
                    "actual_entry_price": pos.actual_entry_price,
                    "sl_price": pos.sl_price,
                    "tp1_price": pos.tp1_price,
                    "tp2_price": pos.tp2_price,
                    "tp3_price": pos.tp3_price,
                    "filled_size": pos.filled_size,
                    "leverage": pos.leverage,
                    "ai_confidence": pos.ai_confidence,
                    "pnl_pct": pos.pnl_pct,
                    "created_at": pos.created_at,
                    "filled_at": pos.filled_at,
                })
            self._data_store.redis.set_positions(json.dumps(positions_data))
        except Exception as e:
            logger.error(f"Failed to update positions cache: {e}")
