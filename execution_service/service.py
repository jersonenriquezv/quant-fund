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

from config.settings import settings
from shared.logger import setup_logger
from shared.models import TradeSetup, RiskApproval
from execution_service.executor import OrderExecutor
from execution_service.monitor import PositionMonitor
from execution_service.models import ManagedPosition

logger = setup_logger("execution_service")


class ExecutionService:
    """Layer 5 — executes approved trades on OKX via ccxt."""

    def __init__(self, risk_service, data_service=None, notifier=None) -> None:
        self._risk = risk_service
        self._data_service = data_service
        self._enabled = bool(settings.OKX_API_KEY)

        if not self._enabled:
            logger.warning(
                "Execution Service DISABLED — OKX_API_KEY not set. "
                "Approved trades will be logged but not executed."
            )
            self._executor = None
            self._monitor = None
        else:
            self._executor = OrderExecutor()
            self._monitor = PositionMonitor(
                self._executor, risk_service, data_store=data_service,
                notifier=notifier
            )
            logger.info("Execution Service initialized")

    async def start(self) -> None:
        """Start the position monitor background loop."""
        if not self._enabled or self._monitor is None:
            return
        # Sync exchange positions BEFORE starting poll loop
        # so adopted positions are registered before monitoring begins.
        await self.sync_exchange_positions()
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
            phase="pending_entry",
            entry_price=setup.entry_price,
            sl_price=setup.sl_price,
            tp1_price=setup.tp1_price,
            tp2_price=setup.tp2_price,
            tp3_price=setup.tp3_price,
            total_size=approval.position_size,
            leverage=approval.leverage,
            entry_order_id=order.get("id"),
            ai_confidence=ai_confidence,
            created_at=int(time.time()),
        )

        self._monitor.register(pos)

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
