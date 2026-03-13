"""
Execution Service facade — single entry point for trade execution.

Owns OrderExecutor and PositionMonitor.
Main method: execute(setup, approval, ai_confidence) → places entry order.

Follows same facade pattern as AIService:
- __init__ checks if enabled (OKX_API_KEY present)
- start() launches background monitor
- stop() cleans up
"""

import time

from config.settings import settings, QUICK_SETUP_TYPES
from shared.logger import setup_logger
from shared.models import TradeSetup, RiskApproval
from execution_service.executor import OrderExecutor
from execution_service.monitor import PositionMonitor
from execution_service.models import ManagedPosition

logger = setup_logger("execution_service")


class ExecutionService:
    """Layer 5 — executes approved trades on OKX via ccxt."""

    def __init__(self, risk_service, data_service=None, alert_manager=None,
                 on_sl_hit=None) -> None:
        self._risk = risk_service
        self._data_service = data_service
        self._alert_manager = alert_manager
        self._enabled = bool(settings.OKX_API_KEY)

        if not self._enabled:
            logger.warning(
                "Execution Service DISABLED — OKX_API_KEY not set. "
                "Approved trades will be logged but not executed."
            )
            self._executor = None
            self._monitor = None
        else:
            self._executor = OrderExecutor(metrics_callback=self._emit_metric)
            self._monitor = PositionMonitor(
                self._executor, risk_service, data_store=data_service,
                alert_manager=alert_manager, on_sl_hit=on_sl_hit
            )
            logger.info("Execution Service initialized")

    def _emit_metric(self, name: str, value: float, pair: str | None = None, labels: dict | None = None) -> None:
        """Write operational metric to PostgreSQL via DataService (fire-and-forget)."""
        if self._data_service is None:
            return
        try:
            self._data_service.postgres.insert_metric(name, value, pair=pair, labels=labels)
        except Exception:
            pass

    async def start(self) -> None:
        """Start the position monitor background loop."""
        if not self._enabled or self._monitor is None:
            return
        # Sync exchange positions BEFORE starting poll loop
        # so adopted positions are registered before monitoring begins.
        await self.sync_exchange_positions()
        await self._reconcile_orphaned_trades()
        self._monitor.start()
        logger.info("Execution Service started")

    async def sync_exchange_positions(self) -> None:
        """Fetch open positions from OKX and adopt any not already tracked.

        Called at startup to detect manually opened positions or positions
        that survived a bot restart. Adopted positions are monitored but
        SL/TP are NOT managed (they stay as-is on the exchange).
        """
        if not self._enabled or self._executor is None or self._monitor is None:
            return

        for pair in settings.TRADING_PAIRS:
            if pair in self._monitor.positions:
                continue  # Already tracking this pair

            pos_data = await self._executor.fetch_position(pair)
            if pos_data is None:
                continue

            contracts = float(pos_data.get("contracts", 0))
            if contracts <= 0:
                continue

            side = pos_data.get("side", "")
            direction = "long" if side == "long" else "short"
            entry_price = float(pos_data.get("entryPrice", 0) or 0)
            leverage = float(pos_data.get("leverage", 1) or 1)
            # ccxt returns contracts count — convert to base currency
            base_size = self._executor.contracts_to_base(pair, contracts)

            adopted = ManagedPosition(
                pair=pair,
                direction=direction,
                setup_type="manual",
                phase="active",
                entry_price=entry_price,
                actual_entry_price=entry_price,
                filled_size=base_size,
                leverage=leverage,
                created_at=int(time.time()),
                filled_at=int(time.time()),
            )

            self._monitor.register(adopted)

            # Notify Risk Service about the existing position
            if self._risk is not None:
                self._risk.on_trade_opened(
                    pair, direction, entry_price, int(time.time())
                )

            logger.info(
                f"Adopted exchange position: {pair} {direction} "
                f"size={contracts} entry={entry_price:.2f} leverage={leverage:.0f}x"
            )

    async def _reconcile_orphaned_trades(self) -> None:
        """Close trades stuck as 'open' in PostgreSQL with no matching exchange position.

        On restart, the monitor loses all in-memory state. Trades that were open
        before the restart remain as status='open' in the DB forever. This method
        detects them by comparing DB records against actual exchange positions
        (already fetched by sync_exchange_positions) and marks orphans as closed.
        """
        if self._data_service is None or self._data_service.postgres is None:
            return
        if self._monitor is None:
            return

        try:
            open_trades = self._data_service.postgres.fetch_open_trades()
        except Exception as e:
            logger.error(f"Failed to fetch open trades for reconciliation: {e}")
            return

        if not open_trades:
            return

        # Pairs that currently have positions on the exchange
        # (already populated by sync_exchange_positions into monitor.positions)
        active_pairs = set(self._monitor.positions.keys())

        reconciled = 0
        for trade in open_trades:
            pair = trade["pair"]
            trade_id = trade["id"]

            if pair in active_pairs:
                continue  # Position exists on exchange — trade is genuinely open

            # No position on exchange → orphaned trade
            try:
                self._data_service.postgres.update_trade(
                    trade_id=trade_id,
                    status="closed",
                    exit_reason="orphaned_restart",
                    pnl_usd=0.0,
                    pnl_pct=0.0,
                )
                reconciled += 1
                logger.warning(
                    f"Reconciled orphaned trade: id={trade_id} {pair} "
                    f"{trade['direction']} — no matching exchange position"
                )
            except Exception as e:
                logger.error(f"Failed to reconcile trade id={trade_id}: {e}")

        if reconciled > 0:
            logger.info(f"Reconciliation complete: {reconciled} orphaned trade(s) closed")

    async def stop(self) -> None:
        """Stop the monitor. Cancel unfilled entries, leave filled positions
        (SL/TP live on exchange and survive bot shutdown)."""
        if self._monitor is not None:
            await self._monitor.stop()
        logger.info("Execution Service stopped")

    async def execute(
        self, setup: TradeSetup, approval: RiskApproval, ai_confidence: float
    ) -> bool:
        """Execute an approved trade.

        1. Configure pair (margin mode + leverage)
        2. Place limit entry order
        3. Register position with monitor for lifecycle management

        Returns True if entry order was placed, False otherwise.
        """
        if not self._enabled or self._executor is None or self._monitor is None:
            logger.warning(
                f"Execution skipped (disabled): {setup.pair} {setup.direction}"
            )
            return False

        # Validate SL/TP price ordering (skip in sandbox — prices are remapped)
        if not settings.OKX_SANDBOX:
            if setup.direction == "long":
                if not (setup.sl_price < setup.entry_price < setup.tp2_price):
                    logger.error(
                        f"Invalid price ordering for LONG: "
                        f"sl={setup.sl_price} entry={setup.entry_price} "
                        f"tp={setup.tp2_price}"
                    )
                    return False
            else:
                if not (setup.sl_price > setup.entry_price > setup.tp2_price):
                    logger.error(
                        f"Invalid price ordering for SHORT: "
                        f"sl={setup.sl_price} entry={setup.entry_price} "
                        f"tp={setup.tp2_price}"
                    )
                    return False

        # Check if already managing a position for this pair
        if setup.pair in self._monitor.positions:
            existing = self._monitor.positions[setup.pair]
            if existing.phase == "pending_entry":
                # Replace stale pending order with this new setup
                logger.info(
                    f"Replacing pending entry: {setup.pair} "
                    f"old={existing.direction}@{existing.entry_price:.2f} → "
                    f"new={setup.direction}@{setup.entry_price:.2f}"
                )
                self._monitor._record_pending_replaced(existing)
                old = await self._monitor.cancel_and_remove_pending(setup.pair)
                if old and self._risk is not None:
                    self._risk.on_trade_cancelled(setup.pair, old.direction)
            elif existing.setup_type == "manual":
                # Adopted position — allow bot to open its own entry alongside it.
                # On OKX net mode, same-direction entries stack.
                # Stop tracking the manual position (user manages their own SL/TP).
                logger.info(
                    f"Adopted position exists for {setup.pair} — "
                    f"allowing new bot entry alongside it"
                )
                self._monitor.positions.pop(setup.pair, None)
            else:
                # Active bot-managed position — don't open a second one
                logger.warning(
                    f"Already managing an active position for {setup.pair} "
                    f"(phase={existing.phase}) — skipping new entry"
                )
                return False

        # Configure pair (margin mode + leverage)
        leverage = int(approval.leverage)
        configured = await self._executor.configure_pair(setup.pair, leverage)
        if not configured:
            logger.error(f"Failed to configure pair: {setup.pair}")
            return False

        # Place entry order (always limit — sandbox uses current price with tolerance)
        # SL/TP are attached to the entry order — OKX auto-creates them on fill.
        side = "buy" if setup.direction == "long" else "sell"
        sl_price = setup.sl_price
        tp_price = setup.tp2_price

        # Validate SL is still valid vs current market price.
        # If price moved past the SL, it would trigger immediately → OKX rejects.
        if not settings.OKX_SANDBOX:
            ticker = await self._executor.fetch_ticker(setup.pair)
            if ticker:
                last = ticker.get("last", 0)
                if last and last > 0:
                    if setup.direction == "short" and sl_price < last:
                        logger.warning(
                            f"Short SL {sl_price:.2f} below market {last:.2f} — "
                            f"would trigger immediately, skipping: {setup.pair}"
                        )
                        return False
                    if setup.direction == "long" and sl_price > last:
                        logger.warning(
                            f"Long SL {sl_price:.2f} above market {last:.2f} — "
                            f"would trigger immediately, skipping: {setup.pair}"
                        )
                        return False

        # Determine if this is a split entry (two limit orders at OB 50% + 75%)
        is_split = (
            setup.entry2_price > 0
            and setup.setup_type not in QUICK_SETUP_TYPES
            and not settings.OKX_SANDBOX
        )
        order2 = None

        # Check minimum order size for split entries
        if is_split:
            half_size = approval.position_size / 2
            min_size = settings.MIN_ORDER_SIZES.get(setup.pair, 0)
            if half_size < min_size:
                logger.info(
                    f"Split entry half size {half_size:.6f} < min {min_size} "
                    f"for {setup.pair} — falling back to single entry"
                )
                is_split = False

        if settings.OKX_SANDBOX:
            # Sandbox: OB entry price may be stale, so use current market price
            # with 0.05% tolerance to get immediate fill without crazy slippage
            ticker = await self._executor.fetch_ticker(setup.pair)
            if ticker is None:
                logger.error(f"Cannot fetch ticker for sandbox entry: {setup.pair}")
                return False
            ask = ticker.get("ask")
            bid = ticker.get("bid")
            if ask is None or bid is None:
                logger.error(f"Ticker missing ask/bid for sandbox entry: {setup.pair}")
                return False
            tolerance = settings.SANDBOX_LIMIT_TOLERANCE_PCT
            if side == "buy":
                entry_price = ask * (1 + tolerance)
            else:
                entry_price = bid * (1 - tolerance)
            logger.info(
                f"Sandbox entry: using current price {entry_price:.2f} "
                f"(ask={ticker.get('ask')}, bid={ticker.get('bid')}, "
                f"original OB entry={setup.entry_price:.2f})"
            )
            order = await self._executor.place_limit_order(
                setup.pair, side, approval.position_size, entry_price,
                sl_trigger_price=sl_price, tp_price=tp_price,
            )
        elif is_split:
            half_size = approval.position_size / 2
            # Entry 1 — OB 50% level
            order = await self._executor.place_limit_order(
                setup.pair, side, half_size, setup.entry_price,
                sl_trigger_price=sl_price, tp_price=tp_price,
            )
            if order is None:
                logger.error(f"Split entry1 placement failed: {setup.pair}")
                return False
            # Entry 2 — OB 75% level (deeper)
            order2 = await self._executor.place_limit_order(
                setup.pair, side, half_size, setup.entry2_price,
                sl_trigger_price=sl_price, tp_price=tp_price,
            )
            if order2 is None:
                logger.warning(
                    f"Split entry2 placement failed: {setup.pair} — "
                    f"proceeding with entry1 at half size"
                )
        else:
            order = await self._executor.place_limit_order(
                setup.pair, side, approval.position_size, setup.entry_price,
                sl_trigger_price=sl_price, tp_price=tp_price,
            )

        if order is None:
            logger.error(f"Entry order placement failed: {setup.pair} {setup.direction}")
            return False

        # Notify Risk Service — on PLACE, not fill (prevents exceeding max positions)
        if self._risk is not None:
            self._risk.on_trade_opened(
                setup.pair, setup.direction, setup.entry_price, int(time.time())
            )

        # Create managed position and register with monitor
        pos = ManagedPosition(
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            setup_id=setup.setup_id,
            phase="pending_entry",
            entry_price=setup.entry_price,
            sl_price=setup.sl_price,
            tp1_price=setup.tp1_price,
            tp2_price=setup.tp2_price,
            total_size=approval.position_size,
            leverage=approval.leverage,
            entry_order_id=order.get("id"),
            ai_confidence=ai_confidence,
            created_at=int(time.time()),
        )

        if is_split:
            pos.is_split_entry = True
            pos.entry2_price = setup.entry2_price
            pos.entry2_order_id = order2.get("id") if order2 else None
            pos.entry1_fill_size = half_size
            pos.entry2_fill_size = half_size if order2 else 0.0

        self._monitor.register(pos)

        # Telegram: order placed
        if self._alert_manager is not None:
            self._monitor._safe_notify(
                self._alert_manager.notify_order_placed(setup, approval)
            )

        if is_split:
            logger.info(
                f"Split trade submitted: {setup.pair} {setup.direction} "
                f"entry1={setup.entry_price:.2f} entry2={setup.entry2_price:.2f} "
                f"half_size={half_size:.6f} leverage={leverage}x "
                f"ai_conf={ai_confidence:.2f}"
            )
        else:
            logger.info(
                f"Trade submitted: {setup.pair} {setup.direction} "
                f"entry={setup.entry_price:.2f} size={approval.position_size:.6f} "
                f"leverage={leverage}x ai_conf={ai_confidence:.2f}"
            )
        return True

    def health(self) -> dict:
        """Return execution service health status."""
        positions = {}
        if self._monitor:
            positions = {
                pair: {"phase": pos.phase, "direction": pos.direction}
                for pair, pos in self._monitor.positions.items()
            }

        return {
            "enabled": self._enabled,
            "active_positions": len(positions),
            "positions": positions,
        }
