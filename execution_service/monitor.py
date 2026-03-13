"""
Position monitor — background async loop that manages position lifecycle.

State machine:

    pending_entry ──[fill]──────> active         (place SL + single TP)
    pending_entry ──[timeout]───> closed         (cancel entry)

    active ──[TP fills]──> closed                (full profit)
    active ──[SL fills]──> closed                (loss or breakeven or trailing)
    active ──[timeout]───> closed                (market close)
    active ──[price >= 1:1 R:R]──> SL moves to breakeven (entry price)
    active ──[price >= 1.5:1 R:R]──> SL moves to tp1_price (trailing)
    active ──[progressive trail]──> SL trails in 0.5 R:R steps (TRAILING_TP_ENABLED)

Exit management:
- SL: stop-market at sl_price for 100% of position
- TP: limit at tp2_price (2:1 R:R) or ceiling TP at 5:1 R:R (progressive trail)
- Legacy mode (TRAILING_TP_ENABLED=False):
    - Breakeven: when price crosses tp1_price (1:1), SL moves to entry price
    - Trailing: when price crosses midpoint(tp1,tp2) (1.5:1), SL moves to tp1_price
- Progressive trail mode (TRAILING_TP_ENABLED=True):
    - SL trails in TRAIL_STEP_RR (0.5) R:R increments, always one step behind
    - Ceiling TP at TRAIL_CEILING_RR (5.0) on exchange as crash protection

Critical safety:
- Entry fill + SL placement fails → EMERGENCY market close
- SL adjustment: place NEW SL first, then cancel OLD SL (zero gap)
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
                 data_store=None, alert_manager=None,
                 on_sl_hit=None) -> None:
        self._executor = executor
        self._risk = risk_service
        self._data_store = data_store  # DataService for DB persistence
        self._alert_manager = alert_manager  # AlertManager (optional)
        self._on_sl_hit = on_sl_hit  # Callback: (pair, sl_price, entry_price) when SL hits
        self._positions: dict[str, ManagedPosition] = {}  # keyed by pair
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Execution metrics (persisted to PostgreSQL via insert_metric)
        self._pending_replaced: int = 0
        self._pending_timeout: int = 0
        self._pending_filled: int = 0
        # Per-setup fill tracking: setup_type -> list of fill times (seconds)
        self._fill_times: dict[str, list[int]] = {}

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
                if pos.is_split_entry and pos.entry2_order_id:
                    await self._executor.cancel_order(pos.entry2_order_id, pair)
                self._close_position(pos, "cancelled")

        logger.info("Position monitor stopped")

    def register(self, pos: ManagedPosition) -> None:
        """Register a new position for monitoring."""
        self._positions[pos.pair] = pos
        self._update_positions_cache()
        logger.info(f"Position registered: {pos.pair} {pos.direction} phase={pos.phase}")

    async def cancel_and_remove_pending(self, pair: str) -> Optional[ManagedPosition]:
        """Cancel a pending entry order and remove from tracking.

        Used when replacing a stale pending order with a better entry.
        Returns the old position if cancelled, None otherwise.
        """
        pos = self._positions.get(pair)
        if pos is None or pos.phase != "pending_entry":
            return None

        cancelled = True
        if pos.entry_order_id:
            ok = await self._executor.cancel_order(pos.entry_order_id, pair)
            if not ok:
                cancelled = False
        if pos.is_split_entry and pos.entry2_order_id:
            ok = await self._executor.cancel_order(pos.entry2_order_id, pair)
            if not ok:
                cancelled = False

        if not cancelled:
            logger.error(
                f"Failed to cancel pending order(s) for {pair} — "
                f"aborting replacement to avoid duplicate orders on exchange"
            )
            return None

        # ML: resolve as replaced
        self._ml_resolve_close(pos, "replaced")

        old_pos = pos
        self._positions.pop(pair, None)
        self._update_positions_cache()

        logger.info(
            f"Pending entry replaced: {pair} {pos.direction} "
            f"entry={pos.entry_price:.2f} (cancelled for new setup)"
        )
        return old_pos

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

            try:
                # Check for dashboard cancel request
                if self._check_cancel_request(pos):
                    continue

                if pos.phase == "pending_entry":
                    await self._check_pending_entry(pos)
                elif pos.phase == "active":
                    await self._check_active_position(pos)
                elif pos.phase == "emergency_pending":
                    await self._retry_emergency_close(pos)
            except Exception as e:
                logger.error(f"Position check error: {pos.pair} {pos.phase} {e}")

    def _check_cancel_request(self, pos: ManagedPosition) -> bool:
        """Check Redis for a dashboard cancel request. Returns True if handled."""
        if self._data_store is None:
            return False
        try:
            if self._data_store.redis.pop_cancel_request(pos.pair):
                logger.info(f"Cancel request from dashboard: {pos.pair} {pos.direction}")
                asyncio.create_task(self._handle_cancel(pos))
                return True
        except Exception as e:
            logger.error(f"Failed to check cancel request: {pos.pair} {e}")
        return False

    async def _handle_cancel(self, pos: ManagedPosition) -> None:
        """Execute a cancel request from the dashboard."""
        if pos.phase == "pending_entry":
            if pos.entry_order_id:
                await self._executor.cancel_order(pos.entry_order_id, pos.pair)
            if pos.is_split_entry and pos.entry2_order_id:
                await self._executor.cancel_order(pos.entry2_order_id, pos.pair)
            self._close_position(pos, "cancelled")
        else:
            await self._close_all_orders_and_market_close(pos)

    # ================================================================
    # State: pending_entry
    # ================================================================

    async def _check_pending_entry(self, pos: ManagedPosition) -> None:
        """Check if entry order filled or timed out."""
        if pos.is_split_entry:
            await self._check_split_pending(pos)
            return

        now = int(time.time())

        # Timeout check — quick setups get shorter timeout
        timeout = (settings.ENTRY_TIMEOUT_QUICK_SECONDS
                   if pos.setup_type in QUICK_SETUP_TYPES
                   else settings.ENTRY_TIMEOUT_SECONDS)
        if now - pos.created_at >= timeout:
            logger.info(f"Entry timeout: {pos.pair} after {timeout}s")
            if pos.entry_order_id:
                await self._executor.cancel_order(pos.entry_order_id, pos.pair)
            self._record_pending_timeout(pos)
            self._close_position(pos, "cancelled")
            return

        # Check entry order status
        if not pos.entry_order_id:
            return
        order = await self._executor.fetch_order(pos.entry_order_id, pos.pair)
        if order is None:
            return

        status = order.get("status", "")
        # ccxt returns 'filled' in contracts for OKX SWAP — convert to base currency
        filled_contracts = float(order.get("filled", 0))
        filled = self._executor.contracts_to_base(pos.pair, filled_contracts)

        if status == "closed" and filled > 0:
            # Fully filled
            actual_price = float(order.get("average", 0) or order.get("price", 0))
            # Debug: log raw order fields to diagnose phantom fills
            # (limit buy at X filled at Y where Y >> X)
            expected = pos.entry_price
            if expected and actual_price and abs(actual_price - expected) / expected > 0.005:
                logger.warning(
                    f"Fill price mismatch: {pos.pair} expected={expected:.2f} "
                    f"actual={actual_price:.2f} diff={abs(actual_price - expected) / expected * 100:.2f}% "
                    f"order_id={pos.entry_order_id} "
                    f"raw={{status={order.get('status')}, average={order.get('average')}, "
                    f"price={order.get('price')}, filled={order.get('filled')}, "
                    f"type={order.get('type')}, side={order.get('side')}, "
                    f"info_avgPx={order.get('info', {}).get('avgPx')}, "
                    f"info_px={order.get('info', {}).get('px')}, "
                    f"info_state={order.get('info', {}).get('state')}}}"
                )
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

    async def _check_split_pending(self, pos: ManagedPosition) -> None:
        """Handle split entry (two limit orders) pending state."""
        now = int(time.time())
        timeout = (settings.ENTRY_TIMEOUT_QUICK_SECONDS
                   if pos.setup_type in QUICK_SETUP_TYPES
                   else settings.ENTRY_TIMEOUT_SECONDS)

        # --- Check entry1 fill ---
        if not pos.entry1_filled and pos.entry_order_id:
            order = await self._executor.fetch_order(pos.entry_order_id, pos.pair)
            if order:
                status = order.get("status", "")
                filled_contracts = float(order.get("filled", 0))
                filled = self._executor.contracts_to_base(pos.pair, filled_contracts)
                if status == "closed" and filled > 0:
                    pos.entry1_filled = True
                    pos.entry1_fill_price = float(order.get("average", 0) or order.get("price", 0))
                    pos.entry1_fill_size = filled
                    logger.info(f"Split entry1 filled: {pos.pair} price={pos.entry1_fill_price:.2f} size={filled:.6f}")
                elif status in ("canceled", "cancelled") and filled <= 0:
                    if pos.entry2_order_id:
                        await self._executor.cancel_order(pos.entry2_order_id, pos.pair)
                    self._close_position(pos, "cancelled")
                    return

        # --- Check entry2 fill (only after entry1 filled) ---
        if pos.entry1_filled and not pos.entry2_filled and pos.entry2_order_id:
            order = await self._executor.fetch_order(pos.entry2_order_id, pos.pair)
            if order:
                status = order.get("status", "")
                filled_contracts = float(order.get("filled", 0))
                filled = self._executor.contracts_to_base(pos.pair, filled_contracts)
                if status == "closed" and filled > 0:
                    pos.entry2_filled = True
                    pos.entry2_fill_price = float(order.get("average", 0) or order.get("price", 0))
                    pos.entry2_fill_size = filled
                    logger.info(f"Split entry2 filled: {pos.pair} price={pos.entry2_fill_price:.2f} size={filled:.6f}")

        # --- Timeout ---
        if now - pos.created_at >= timeout:
            if not pos.entry1_filled and pos.entry_order_id:
                await self._executor.cancel_order(pos.entry_order_id, pos.pair)
            if not pos.entry2_filled and pos.entry2_order_id:
                await self._executor.cancel_order(pos.entry2_order_id, pos.pair)
            if pos.entry1_filled:
                logger.info(f"Split entry2 timeout: {pos.pair} — activating with entry1 only")
                pos.actual_entry_price = pos.entry1_fill_price
                pos.filled_size = pos.entry1_fill_size
                pos.filled_at = int(time.time())
                pos.is_split_entry = False
                await self._on_entry_filled(pos)
            else:
                self._record_pending_timeout(pos)
                self._close_position(pos, "cancelled")
            return

        # --- Both filled → VWAP consolidation ---
        if pos.entry1_filled and pos.entry2_filled:
            total_size = pos.entry1_fill_size + pos.entry2_fill_size
            vwap = ((pos.entry1_fill_price * pos.entry1_fill_size + pos.entry2_fill_price * pos.entry2_fill_size) / total_size) if total_size > 0 else pos.entry1_fill_price
            logger.info(f"Split entry VWAP: {pos.pair} e1={pos.entry1_fill_price:.2f}×{pos.entry1_fill_size:.6f} + e2={pos.entry2_fill_price:.2f}×{pos.entry2_fill_size:.6f} → vwap={vwap:.2f} total={total_size:.6f}")
            await self._cancel_attached_orders(pos)
            pos.actual_entry_price = vwap
            pos.filled_size = total_size
            pos.filled_at = int(time.time())
            pos.is_split_entry = False
            await self._on_entry_filled(pos)
            return

        # --- Entry1 filled, entry2 still pending ---
        if pos.entry1_filled and not pos.entry2_filled and pos.entry2_order_id:
            order = await self._executor.fetch_order(pos.entry2_order_id, pos.pair)
            if order and order.get("status") in ("canceled", "cancelled"):
                filled_contracts = float(order.get("filled", 0))
                filled = self._executor.contracts_to_base(pos.pair, filled_contracts)
                if filled <= 0:
                    logger.info(f"Split entry2 cancelled: {pos.pair} — activating with entry1 only")
                    pos.actual_entry_price = pos.entry1_fill_price
                    pos.filled_size = pos.entry1_fill_size
                    pos.filled_at = int(time.time())
                    pos.is_split_entry = False
                    await self._on_entry_filled(pos)

    async def _cancel_attached_orders(self, pos: ManagedPosition) -> None:
        """Cancel all pending algo/TP orders for VWAP consolidation."""
        algos = await self._executor.find_pending_algo_orders(pos.pair)
        for algo in algos:
            algo_id = algo.get("algoId", "")
            if algo_id:
                await self._executor.cancel_algo_order(algo_id, pos.pair)
        close_side = "sell" if pos.direction == "long" else "buy"
        open_orders = await self._executor.fetch_open_orders(pos.pair)
        for o in open_orders:
            if o.get("reduceOnly") and o.get("side") == close_side:
                order_id = o.get("id", "")
                if order_id:
                    await self._executor.cancel_order(order_id, pos.pair)

    async def _on_entry_filled(self, pos: ManagedPosition) -> None:
        """Entry filled — find attached SL/TP or place manually. If SL fails → emergency close."""
        self._record_pending_filled(pos)
        close_side = "sell" if pos.direction == "long" else "buy"

        # In sandbox mode, remap SL/TP relative to actual fill price
        # (sandbox prices differ from real market; preserve R:R ratios)
        if settings.OKX_SANDBOX and pos.actual_entry_price and pos.entry_price:
            self._remap_sandbox_prices(pos)

        # Try to find attached SL/TP orders (created by OKX when entry filled).
        # Wait briefly for OKX to create them.
        sl_found, tp_found = False, False
        await asyncio.sleep(0.5)
        algos = await self._executor.find_pending_algo_orders(pos.pair)

        for algo in algos:
            # OKX uses 'triggerPx' for trigger orders, 'slTriggerPx' for conditional/attached
            trigger_px = float(algo.get("slTriggerPx", 0) or algo.get("triggerPx", 0) or 0)
            algo_id = algo.get("algoId", "")

            # Match SL by trigger price (within 0.5% tolerance)
            if not sl_found and trigger_px > 0 and pos.sl_price > 0:
                diff_pct = abs(trigger_px - pos.sl_price) / pos.sl_price
                if diff_pct < 0.005:
                    pos.sl_order_id = algo_id
                    pos.current_sl_price = trigger_px
                    sl_found = True
                    logger.info(
                        f"Attached SL found: {pos.pair} algoId={algo_id} "
                        f"trigger={trigger_px:.2f}"
                    )

        # Also check for TP in pending limit orders (reduceOnly)
        if not tp_found:
            tp_found = await self._find_attached_tp(pos)

        # Fallback: place SL manually if attached not found
        if not sl_found:
            logger.info(f"No attached SL found, placing manually: {pos.pair}")
            sl_order = None
            for attempt in range(3):
                sl_order = await self._executor.place_stop_market(
                    pos.pair, close_side, pos.filled_size, pos.sl_price
                )
                if sl_order is not None:
                    break
                if attempt < 2:
                    delay = 0.3 * (attempt + 1)
                    logger.warning(
                        f"SL placement attempt {attempt + 1}/3 failed, "
                        f"retrying in {delay}s: {pos.pair}"
                    )
                    await asyncio.sleep(delay)

            if sl_order is None:
                logger.error(f"SL placement FAILED after entry fill — EMERGENCY CLOSE: {pos.pair}")
                if self._alert_manager is not None:
                    self._safe_notify(
                        self._alert_manager.notify_emergency(pos, "SL placement failed — market closed")
                    )
                result = await self._executor.close_position_market(
                    pos.pair, close_side, pos.filled_size
                )
                if result is None:
                    logger.critical(f"EMERGENCY CLOSE FAILED: {pos.pair} — will retry on next poll cycle")
                    pos.phase = "emergency_pending"
                    pos.emergency_retries = 1
                    return
                close_price = self._extract_close_price(result, pos)
                if close_price is not None:
                    self._calculate_pnl(pos, close_price)
                self._close_position(pos, "emergency")
                return

            pos.sl_order_id = sl_order.get("id")
            pos.current_sl_price = pos.sl_price

        # Fallback: place TP manually if attached not found
        if not tp_found:
            logger.info(f"No attached TP found, placing manually: {pos.pair}")
            tp = await self._executor.place_take_profit(
                pos.pair, close_side, pos.filled_size, pos.tp2_price
            )
            if tp is None:
                logger.error(
                    f"TP placement failed: {pos.pair} — "
                    f"position stays open with SL only (no TP on exchange)"
                )
            pos.tp_order_id = tp.get("id") if tp else None

        # Slippage guard — close if entry fill deviates too much from intended price.
        # Skip in sandbox (synthetic fills have arbitrary slippage).
        if (not settings.OKX_SANDBOX
                and pos.actual_entry_price and pos.entry_price
                and pos.entry_price > 0):
            slippage_pct = abs(pos.actual_entry_price - pos.entry_price) / pos.entry_price
            if slippage_pct > settings.MAX_SLIPPAGE_PCT:
                logger.warning(
                    f"Excessive slippage: {pos.pair} "
                    f"expected={pos.entry_price:.2f} actual={pos.actual_entry_price:.2f} "
                    f"slippage={slippage_pct*100:.3f}% > max {settings.MAX_SLIPPAGE_PCT*100:.1f}% "
                    f"— closing immediately"
                )
                if pos.sl_order_id:
                    await self._executor.cancel_order(pos.sl_order_id, pos.pair)
                if pos.tp_order_id:
                    await self._executor.cancel_order(pos.tp_order_id, pos.pair)
                result = await self._executor.close_position_market(
                    pos.pair, close_side, pos.filled_size
                )
                if result is None:
                    logger.error(f"Slippage close FAILED: {pos.pair} — emergency pending")
                    pos.phase = "emergency_pending"
                    pos.emergency_retries = 1
                    return
                close_price = self._extract_close_price(result, pos)
                if close_price is not None:
                    self._calculate_pnl(pos, close_price)
                self._close_position(pos, "excessive_slippage")
                return

        # Post-fill SL distance validation.
        # Slippage can shrink the effective SL distance below the minimum.
        # If so, close immediately rather than holding a micro-SL position.
        if pos.actual_entry_price and pos.actual_entry_price > 0:
            sl_price = pos.current_sl_price or pos.sl_price
            actual_risk_pct = abs(pos.actual_entry_price - sl_price) / pos.actual_entry_price
            if actual_risk_pct < settings.MIN_RISK_DISTANCE_PCT:
                logger.warning(
                    f"Post-fill SL too close: {pos.pair} "
                    f"entry={pos.actual_entry_price:.2f} sl={sl_price:.2f} "
                    f"risk={actual_risk_pct*100:.3f}% < min {settings.MIN_RISK_DISTANCE_PCT*100:.1f}% "
                    f"— closing immediately"
                )
                # Cancel SL and TP, then market close
                if pos.sl_order_id:
                    await self._executor.cancel_order(pos.sl_order_id, pos.pair)
                if pos.tp_order_id:
                    await self._executor.cancel_order(pos.tp_order_id, pos.pair)
                result = await self._executor.close_position_market(
                    pos.pair, close_side, pos.filled_size
                )
                if result is None:
                    logger.error(f"Post-fill close FAILED: {pos.pair} — emergency pending")
                    pos.phase = "emergency_pending"
                    pos.emergency_retries = 1
                    return
                close_price = self._extract_close_price(result, pos)
                if close_price is not None:
                    self._calculate_pnl(pos, close_price)
                self._close_position(pos, "sl_too_close")
                return

        pos.phase = "active"
        self._update_positions_cache()
        logger.info(f"Position ACTIVE: {pos.pair} {pos.direction} "
                     f"entry={pos.actual_entry_price} size={pos.filled_size:.6f} "
                     f"sl={pos.current_sl_price:.2f} tp={pos.tp2_price:.2f} "
                     f"sl_attached={sl_found} tp_attached={tp_found}")

        # Persist trade to PostgreSQL
        self._persist_trade_open(pos)

    async def _find_attached_tp(self, pos: ManagedPosition) -> bool:
        """Find attached TP order in open limit orders (reduceOnly).

        OKX attached TP orders appear as regular limit orders, not algo orders.
        """
        symbol = f"{pos.pair}:USDT"
        try:
            orders = await self._executor._run_sync(
                self._executor._exchange.fetch_open_orders, symbol
            )
            for order in orders:
                order_price = float(order.get("price", 0) or 0)
                if order_price > 0 and pos.tp2_price > 0:
                    diff_pct = abs(order_price - pos.tp2_price) / pos.tp2_price
                    if diff_pct < 0.005:
                        pos.tp_order_id = order.get("id")
                        logger.info(
                            f"Attached TP found: {pos.pair} orderId={order.get('id')} "
                            f"price={order_price:.2f}"
                        )
                        return True
        except Exception as e:
            logger.error(f"Find attached TP error: {pos.pair} {e}")
        return False

    # ================================================================
    # State: active
    # ================================================================

    async def _check_active_position(self, pos: ManagedPosition) -> None:
        """Check SL, TP, breakeven trigger, and timeout."""
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

        # Adopted positions (manual/external) — no SL order ID.
        # Poll exchange directly to detect when position is closed.
        if not pos.sl_order_id and not pos.tp_order_id:
            exchange_pos = await self._executor.fetch_position(pos.pair)
            if exchange_pos is None:
                # Network error — don't close, just skip this cycle
                return
            if float(exchange_pos.get("contracts", 0)) <= 0:
                logger.info(f"Adopted position closed on exchange: {pos.pair} {pos.direction}")
                self._close_position(pos, "manual_close")
                return
            return  # Still open, nothing to manage

        # Check SL
        if pos.sl_order_id:
            sl_status = await self._executor.fetch_order(pos.sl_order_id, pos.pair)
            if sl_status and sl_status.get("status") == "closed":
                logger.info(f"SL hit: {pos.pair} {pos.direction}")
                await self._cancel_tp(pos)
                sl_exit = pos.current_sl_price or pos.sl_price
                self._calculate_pnl(pos, float(sl_status.get("average", 0) or sl_exit))
                # Mark OB as failed if it was a real loss (not breakeven)
                if self._on_sl_hit and pos.pnl_pct < 0:
                    try:
                        self._on_sl_hit(pos.pair, pos.sl_price, pos.entry_price)
                    except Exception as e:
                        logger.error(f"on_sl_hit callback error: {pos.pair} {e}")
                self._close_position(pos, "sl")
                return
            if sl_status and sl_status.get("status") == "canceled":
                # SL was cancelled — check if position still exists on exchange
                exchange_pos = await self._executor.fetch_position(pos.pair)
                pos_contracts = float(exchange_pos.get("contracts", 0)) if exchange_pos else 0

                if exchange_pos is not None and pos_contracts <= 0:
                    # Position closed manually on exchange — clean up
                    logger.info(
                        f"SL cancelled + position gone on exchange: {pos.pair} "
                        f"— user closed manually"
                    )
                    await self._cancel_tp(pos)
                    # Use current market price for PnL estimate
                    ticker = await self._executor.fetch_ticker(pos.pair)
                    exit_price = float(ticker.get("last", 0)) if ticker else 0
                    if exit_price > 0:
                        self._calculate_pnl(pos, exit_price)
                    self._close_position(pos, "manual_close")
                    return

                # Position still open — re-place SL as before
                logger.warning(f"SL order cancelled externally: {pos.pair} — re-placing")
                close_side = "sell" if pos.direction == "long" else "buy"
                sl_price = pos.current_sl_price or pos.sl_price
                new_sl = await self._executor.place_stop_market(
                    pos.pair, close_side, pos.filled_size, sl_price
                )
                if new_sl is not None:
                    pos.sl_order_id = new_sl.get("id")
                    pos.sl_fetch_failures = 0
                else:
                    logger.error(f"Failed to re-place SL after cancel: {pos.pair}")
                return
            if sl_status is None:
                # SL order fetch failed — track consecutive failures
                pos.sl_fetch_failures += 1
                if pos.sl_fetch_failures >= 12:  # ~60s at 5s poll interval
                    await self._handle_sl_vanished(pos)
                    return
            else:
                pos.sl_fetch_failures = 0

        # Check TP
        if pos.tp_order_id:
            tp_status = await self._executor.fetch_order(pos.tp_order_id, pos.pair)
            if tp_status and tp_status.get("status") == "closed":
                logger.info(f"TP hit: {pos.pair} {pos.direction} at 2:1 R:R")
                # Cancel SL since position is fully closed
                if pos.sl_order_id:
                    await self._executor.cancel_order(pos.sl_order_id, pos.pair)
                tp_exit = float(tp_status.get("average", 0) or pos.tp2_price)
                self._calculate_pnl(pos, tp_exit)
                self._close_position(pos, "tp")
                return

        # SL management — progressive trail or legacy breakeven+trailing
        if pos.actual_entry_price:
            if settings.TRAILING_TP_ENABLED:
                await self._check_progressive_trail(pos)
            else:
                if not pos.breakeven_hit:
                    await self._check_breakeven(pos)
                if pos.breakeven_hit and not pos.trailing_sl_moved:
                    await self._check_trailing_sl(pos)

    # ================================================================
    # Breakeven trigger — move SL to entry when price crosses 1:1 R:R
    # ================================================================

    async def _check_breakeven(self, pos: ManagedPosition) -> None:
        """Check if price has crossed the 1:1 R:R level (tp1_price).
        If so, move SL to breakeven (entry price).
        """
        ticker = await self._executor.fetch_ticker(pos.pair)
        if ticker is None:
            return

        current_price = float(ticker.get("last", 0) or 0)
        if current_price <= 0:
            return

        triggered = False
        if pos.direction == "long" and current_price >= pos.tp1_price:
            triggered = True
        elif pos.direction == "short" and current_price <= pos.tp1_price:
            triggered = True

        if triggered:
            logger.info(
                f"Breakeven triggered: {pos.pair} {pos.direction} "
                f"price={current_price:.2f} >= tp1={pos.tp1_price:.2f} "
                f"→ moving SL to entry={pos.actual_entry_price:.2f}"
            )
            await self._adjust_sl(pos, pos.actual_entry_price)
            pos.breakeven_hit = True

    # ================================================================
    # Trailing SL — move SL to tp1 when price crosses 1.5:1 R:R
    # ================================================================

    async def _check_trailing_sl(self, pos: ManagedPosition) -> None:
        """Check if price has crossed the 1.5:1 R:R level (midpoint of tp1 and tp2).
        If so, move SL to tp1_price.
        """
        if pos.tp1_price <= 0 or pos.tp2_price <= 0:
            return

        midpoint = (pos.tp1_price + pos.tp2_price) / 2.0

        ticker = await self._executor.fetch_ticker(pos.pair)
        if ticker is None:
            return

        current_price = float(ticker.get("last", 0) or 0)
        if current_price <= 0:
            return

        triggered = False
        if pos.direction == "long" and current_price >= midpoint:
            triggered = True
        elif pos.direction == "short" and current_price <= midpoint:
            triggered = True

        if triggered:
            logger.info(
                f"Trailing SL triggered: {pos.pair} {pos.direction} "
                f"price={current_price:.2f} >= midpoint={midpoint:.2f} "
                f"→ moving SL to tp1={pos.tp1_price:.2f}"
            )
            await self._adjust_sl(pos, pos.tp1_price)
            pos.trailing_sl_moved = True

    # ================================================================
    # Progressive trailing SL — unified trail in TRAIL_STEP_RR increments
    # ================================================================

    async def _check_progressive_trail(self, pos: ManagedPosition) -> None:
        """Trail SL in 0.5 R:R steps. SL always trails one step behind.

        Level 0 = no trail (SL at original).
        Level 1 = breakeven (SL at entry).
        Level 2 = SL at 1.0 R:R level.
        Level N = SL at (N-1) * TRAIL_STEP_RR R:R level.
        """
        risk = abs(pos.actual_entry_price - pos.sl_price)
        if risk <= 0:
            return

        ticker = await self._executor.fetch_ticker(pos.pair)
        if ticker is None:
            return

        current_price = float(ticker.get("last", 0) or 0)
        if current_price <= 0:
            return

        # Calculate current R:R from price movement
        if pos.direction == "long":
            current_rr = (current_price - pos.actual_entry_price) / risk
        else:
            current_rr = (pos.actual_entry_price - current_price) / risk

        if current_rr < settings.TRAIL_ACTIVATION_RR:
            return

        # Determine trail level: how many full steps price has crossed
        level = int(current_rr / settings.TRAIL_STEP_RR)
        if level <= pos.trail_level:
            return

        # Calculate new SL: one step behind current level
        sl_rr = (level - 1) * settings.TRAIL_STEP_RR
        if pos.direction == "long":
            new_sl = pos.actual_entry_price + (risk * sl_rr)
        else:
            new_sl = pos.actual_entry_price - (risk * sl_rr)

        logger.info(
            f"Progressive trail: {pos.pair} {pos.direction} "
            f"price={current_price:.2f} R:R={current_rr:.2f} "
            f"level {pos.trail_level}→{level} "
            f"SL→{new_sl:.2f} (locks {sl_rr:.1f}R)"
        )

        await self._adjust_sl(pos, new_sl)
        pos.trail_level = level

        # Keep breakeven_hit compatible with dashboard/logging
        if not pos.breakeven_hit:
            pos.breakeven_hit = True

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
            close_price = self._extract_close_price(result, pos)
            if close_price is not None:
                self._calculate_pnl(pos, close_price)
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
    # SL vanished fallback — check exchange position when SL order
    # disappears from OKX's queryable states
    # ================================================================

    async def _handle_sl_vanished(self, pos: ManagedPosition) -> None:
        """Fallback when SL algo order can't be found for 60+ seconds.

        Checks the exchange position directly:
        - Position gone → SL was triggered, close in monitor
        - Position exists → SL disappeared, re-place it
        """
        exchange_pos = await self._executor.fetch_position(pos.pair)

        if exchange_pos is None:
            # Network error — don't act, try again next cycle
            logger.warning(
                f"SL vanished check: network error fetching position: {pos.pair}"
            )
            return

        contracts = float(exchange_pos.get("contracts", 0))
        if contracts <= 0:
            # Position closed on exchange — SL was triggered
            sl_price = pos.current_sl_price or pos.sl_price
            logger.info(
                f"SL order vanished, exchange position closed: {pos.pair} "
                f"(SL likely triggered at ~{sl_price:.2f})"
            )
            await self._cancel_tp(pos)
            self._calculate_pnl(pos, sl_price)
            if self._on_sl_hit and pos.pnl_pct < 0:
                try:
                    self._on_sl_hit(pos.pair, pos.sl_price, pos.entry_price)
                except Exception as e:
                    logger.error(f"on_sl_hit callback error: {pos.pair} {e}")
            self._close_position(pos, "sl")
        else:
            # Position still open but SL order disappeared — re-place SL
            logger.warning(
                f"SL order vanished but position still open: {pos.pair} "
                f"({contracts} contracts) — re-placing SL"
            )
            close_side = "sell" if pos.direction == "long" else "buy"
            sl_price = pos.current_sl_price or pos.sl_price
            new_sl = await self._executor.place_stop_market(
                pos.pair, close_side, pos.filled_size, sl_price
            )
            if new_sl is not None:
                pos.sl_order_id = new_sl.get("id")
                pos.sl_fetch_failures = 0
                logger.info(f"SL re-placed: {pos.pair} trigger={sl_price:.2f}")
            else:
                logger.error(
                    f"CRITICAL: Cannot re-place SL: {pos.pair} — "
                    f"emergency close next cycle"
                )
                # Force emergency close on next cycle
                pos.phase = "emergency_pending"
                pos.emergency_retries = 0

    # ================================================================
    # SL adjustment — new SL first, then cancel old (zero gap)
    # ================================================================

    async def _adjust_sl(self, pos: ManagedPosition, new_price: float) -> None:
        """Move SL to new level. Place new BEFORE cancelling old.

        Zero gap: both SLs exist briefly. In net mode, second SL closing
        zero remaining contracts is a no-op.
        """
        close_side = "sell" if pos.direction == "long" else "buy"

        # Place new SL first
        new_sl = await self._executor.place_stop_market(
            pos.pair, close_side, pos.filled_size, new_price
        )

        if new_sl is None:
            logger.error(f"New SL placement failed — keeping old SL: {pos.pair}")
            return

        # Cancel old SL
        old_sl_id = pos.sl_order_id
        if old_sl_id:
            await self._executor.cancel_order(old_sl_id, pos.pair)

        pos.sl_order_id = new_sl.get("id")
        pos.current_sl_price = new_price
        pos.sl_fetch_failures = 0
        logger.info(f"SL adjusted: {pos.pair} new_price={new_price:.2f}")

    # ================================================================
    # Helpers
    # ================================================================

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

        logger.info(
            f"Sandbox price remap: {pos.pair} ratio={ratio:.4f} "
            f"sl={old_sl:.2f}->{pos.sl_price:.2f} "
            f"be_trigger={pos.tp1_price:.2f} tp={pos.tp2_price:.2f}"
        )

    async def _cancel_tp(self, pos: ManagedPosition) -> None:
        """Cancel the TP order if it exists."""
        if pos.tp_order_id:
            await self._executor.cancel_order(pos.tp_order_id, pos.pair)

    async def _close_all_orders_and_market_close(self, pos: ManagedPosition) -> None:
        """Cancel all orders and market close the position (timeout)."""
        # Cancel SL and TP
        if pos.sl_order_id:
            await self._executor.cancel_order(pos.sl_order_id, pos.pair)
        await self._cancel_tp(pos)

        # Market close remaining
        close_side = "sell" if pos.direction == "long" else "buy"
        if pos.filled_size > 0:
            result = await self._executor.close_position_market(pos.pair, close_side, pos.filled_size)
            if result:
                close_price = self._extract_close_price(result, pos)
                if close_price is not None:
                    self._calculate_pnl(pos, close_price)

        self._close_position(pos, "timeout")

    def _emit_metric(self, name: str, value: float,
                     pair: str | None = None,
                     labels: dict | None = None) -> None:
        """Write execution metric to PostgreSQL (fire-and-forget)."""
        if self._data_store is None:
            return
        try:
            self._data_store.postgres.insert_metric(name, value, pair=pair, labels=labels)
        except Exception:
            pass

    def _record_pending_replaced(self, pos: ManagedPosition) -> None:
        """Record a pending entry being replaced by a new setup."""
        self._pending_replaced += 1
        self._emit_metric("pending_replaced", 1, pos.pair,
                          {"setup_type": pos.setup_type})

    def _record_pending_timeout(self, pos: ManagedPosition) -> None:
        """Record a pending entry that timed out without filling."""
        self._pending_timeout += 1
        self._emit_metric("pending_timeout", 1, pos.pair,
                          {"setup_type": pos.setup_type})

    def _record_pending_filled(self, pos: ManagedPosition) -> None:
        """Record a pending entry that filled, with time-to-fill."""
        self._pending_filled += 1
        fill_time = (pos.filled_at or int(time.time())) - pos.created_at
        setup = pos.setup_type or "unknown"
        if setup not in self._fill_times:
            self._fill_times[setup] = []
        self._fill_times[setup].append(fill_time)
        self._emit_metric("pending_filled", 1, pos.pair,
                          {"setup_type": setup})
        self._emit_metric("time_to_fill_seconds", fill_time, pos.pair,
                          {"setup_type": setup})
        # Log fill rate summary
        total = self._pending_filled + self._pending_timeout + self._pending_replaced
        if total > 0:
            fill_rate = self._pending_filled / total
            logger.info(
                f"Execution stats: filled={self._pending_filled} "
                f"timeout={self._pending_timeout} replaced={self._pending_replaced} "
                f"fill_rate={fill_rate:.1%} avg_fill_time_{setup}="
                f"{sum(self._fill_times.get(setup, [0])) / max(len(self._fill_times.get(setup, [1])), 1):.0f}s"
            )

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

    def _extract_close_price(self, result, pos: ManagedPosition) -> float | None:
        """Extract close price from market close result, with fallback to entry."""
        if not pos.actual_entry_price:
            return None
        if isinstance(result, dict):
            return float(result.get("average", 0) or pos.actual_entry_price)
        return pos.actual_entry_price

    def _calculate_pnl(self, pos: ManagedPosition, exit_price: float) -> None:
        """Calculate PnL for the full position at exit_price, net of fees."""
        if not pos.actual_entry_price or pos.actual_entry_price == 0:
            return

        pos.actual_exit_price = exit_price

        if pos.direction == "long":
            pnl_usd = (exit_price - pos.actual_entry_price) * pos.filled_size
        else:
            pnl_usd = (pos.actual_entry_price - exit_price) * pos.filled_size

        # Deduct entry + exit fees
        entry_notional = pos.actual_entry_price * pos.filled_size
        exit_notional = exit_price * pos.filled_size
        total_fees = (entry_notional + exit_notional) * settings.TRADING_FEE_RATE
        pnl_usd -= total_fees

        if entry_notional > 0:
            pos.pnl_pct = pnl_usd / entry_notional
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
        if self._alert_manager is not None and reason != "cancelled":
            self._safe_notify(self._alert_manager.notify_trade_closed(pos))

        # Persist trade close to PostgreSQL
        self._persist_trade_close(pos)

        # ML: resolve outcome
        self._ml_resolve_close(pos, reason)

        # Update Redis positions cache
        self._update_positions_cache()

        # Notify Risk Service
        if self._risk is not None:
            if reason == "cancelled":
                # Pending entry cancelled — remove from open count without PnL impact
                self._risk.on_trade_cancelled(pos.pair, pos.direction)
            else:
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
                tp3_price=0.0,
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
                actual_exit=pos.actual_exit_price,
                exit_reason=pos.close_reason,
                pnl_usd=pnl_usd,
                pnl_pct=pos.pnl_pct,
                status="closed",
            )
        except Exception as e:
            logger.error(f"Failed to persist trade close: {pos.pair} {e}")

    def _ml_resolve_close(self, pos: ManagedPosition, reason: str) -> None:
        """Resolve ML setup outcome on position close (fire-and-forget)."""
        if not pos.setup_id or self._data_store is None or self._data_store.postgres is None:
            return
        try:
            # Map close_reason to outcome_type
            outcome_map = {
                "tp": "filled_tp",
                "sl": "filled_sl",
                "breakeven_sl": "filled_sl",
                "trailing_sl": "filled_trailing",
                "timeout": "filled_timeout",
                "emergency": "filled_timeout",
                "excessive_slippage": "filled_timeout",
                "sl_too_close": "filled_timeout",
                "cancelled": "unfilled_timeout",
                "replaced": "replaced",
            }
            outcome_type = outcome_map.get(reason, "filled_timeout")

            # Compute durations
            fill_duration_ms = None
            trade_duration_ms = None
            if pos.filled_at and pos.created_at:
                fill_duration_ms = (pos.filled_at - pos.created_at) * 1000
            if pos.closed_at and pos.filled_at:
                trade_duration_ms = (pos.closed_at - pos.filled_at) * 1000

            # PnL
            pnl_usd = None
            if pos.actual_entry_price and pos.filled_size:
                pnl_usd = pos.actual_entry_price * pos.filled_size * pos.pnl_pct

            ok = self._data_store.postgres.update_ml_setup_outcome(
                setup_id=pos.setup_id,
                outcome_type=outcome_type,
                pnl_pct=pos.pnl_pct if pos.pnl_pct != 0 else None,
                pnl_usd=pnl_usd,
                actual_entry=pos.actual_entry_price,
                actual_exit=pos.actual_exit_price,
                exit_reason=reason,
                fill_duration_ms=fill_duration_ms,
                trade_duration_ms=trade_duration_ms,
            )
            self._emit_metric(
                "ml_outcome_update_ok" if ok else "ml_outcome_update_error", 1, pos.pair
            )
        except Exception as e:
            logger.error(f"ML outcome resolution failed: {pos.pair} {e}")
            self._emit_metric("ml_outcome_update_error", 1, pos.pair)

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
                    "sl_price": pos.current_sl_price or pos.sl_price,
                    "tp_price": pos.tp2_price,
                    "filled_size": pos.filled_size,
                    "leverage": pos.leverage,
                    "ai_confidence": pos.ai_confidence,
                    "pnl_pct": pos.pnl_pct,
                    "breakeven_hit": pos.breakeven_hit,
                    "trailing_sl_moved": pos.trailing_sl_moved,
                    "created_at": pos.created_at,
                    "filled_at": pos.filled_at,
                })
            self._data_store.redis.set_positions(json.dumps(positions_data))
        except Exception as e:
            logger.error(f"Failed to update positions cache: {e}")
