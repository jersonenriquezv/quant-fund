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

    def __init__(self, risk_service) -> None:
        self._risk = risk_service
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
            self._monitor = PositionMonitor(self._executor, risk_service)
            logger.info("Execution Service initialized")

    async def start(self) -> None:
        """Start the position monitor background loop."""
        if not self._enabled or self._monitor is None:
            return
        self._monitor.start()
        logger.info("Execution Service started")

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

        # Check if already managing a position for this pair
        if setup.pair in self._monitor.positions:
            logger.warning(
                f"Already managing a position for {setup.pair} — skipping new entry"
            )
            return False

        # Configure pair (margin mode + leverage)
        leverage = int(approval.leverage)
        configured = await self._executor.configure_pair(setup.pair, leverage)
        if not configured:
            logger.error(f"Failed to configure pair: {setup.pair}")
            return False

        # Place limit entry order
        side = "buy" if setup.direction == "long" else "sell"
        order = await self._executor.place_limit_order(
            setup.pair, side, approval.position_size, setup.entry_price
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
