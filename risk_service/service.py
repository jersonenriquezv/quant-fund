"""
Risk Service facade — single entry point for trade risk evaluation.

Owns PositionSizer, Guardrails, and RiskStateTracker.
Main method: check(setup) -> RiskApproval.
"""

import time

from config.settings import settings
from shared.logger import setup_logger
from shared.models import TradeSetup, RiskApproval
from risk_service.position_sizer import PositionSizer
from risk_service.guardrails import Guardrails
from risk_service.state_tracker import RiskStateTracker

logger = setup_logger("risk_service")


class RiskService:
    """Layer 4 — enforces non-negotiable guardrails before trade execution."""

    def __init__(self, capital: float, data_service=None) -> None:
        self._sizer = PositionSizer()
        self._guardrails = Guardrails()
        self._state = RiskStateTracker(capital)
        self._data_service = data_service
        self._persist_failures: int = 0
        logger.info(f"Risk Service initialized with capital=${capital:.2f}")

    # ================================================================
    # Main entry point
    # ================================================================

    def check(self, setup: TradeSetup) -> RiskApproval:
        """Run all guardrails and calculate position size.

        Fails fast — first guardrail failure rejects the trade.
        """
        now = int(time.time())

        # --- Guardrail checks (fail fast) ---
        checks = [
            self._guardrails.check_min_risk_distance(setup),
            self._guardrails.check_rr_ratio(setup),
            self._guardrails.check_cooldown(
                self._state.get_last_loss_time(), now
            ),
            self._guardrails.check_max_trades_today(
                self._state.get_trades_today_count()
            ),
            self._guardrails.check_max_open_positions(
                self._state.get_open_positions_count()
            ),
            self._guardrails.check_daily_drawdown(
                self._state.get_daily_dd_pct()
            ),
            self._guardrails.check_weekly_drawdown(
                self._state.get_weekly_dd_pct()
            ),
        ]

        for passed, reason in checks:
            if not passed:
                logger.warning(f"Trade REJECTED: {reason} | {setup.pair} {setup.direction}")
                self._persist_risk_event("guardrail_rejected", {
                    "pair": setup.pair,
                    "direction": setup.direction,
                    "reason": reason,
                })
                return RiskApproval(
                    approved=False,
                    position_size=0.0,
                    leverage=0.0,
                    risk_pct=0.0,
                    reason=reason,
                )

        # --- Position sizing ---
        # Notional = TRADE_CAPITAL_PCT % of balance. Leverage = MAX_LEVERAGE.
        # e.g. 15% of $106 = $15.90 notional, at 5x leverage = $3.18 margin.
        capital = self._state.get_capital()
        notional = capital * settings.TRADE_CAPITAL_PCT
        leverage = float(settings.MAX_LEVERAGE)
        position_size = notional / setup.entry_price

        if position_size <= 0 or capital <= 0:
            logger.warning(f"Trade REJECTED: position sizing error: capital={capital}")
            return RiskApproval(
                approved=False,
                position_size=0.0,
                leverage=0.0,
                risk_pct=0.0,
                reason=f"Position sizing error: capital={capital}",
            )

        # --- Exchange minimum order size check ---
        min_size = settings.MIN_ORDER_SIZES.get(setup.pair, 0)
        if min_size > 0 and position_size < min_size:
            reason = (
                f"Position size {position_size:.6f} below exchange minimum "
                f"{min_size} for {setup.pair} "
                f"(need ${min_size * setup.entry_price:.0f} notional, "
                f"have ${position_size * setup.entry_price:.0f})"
            )
            logger.warning(f"Trade REJECTED: {reason}")
            return RiskApproval(
                approved=False,
                position_size=0.0,
                leverage=0.0,
                risk_pct=0.0,
                reason=reason,
            )

        risk_pct = settings.TRADE_CAPITAL_PCT
        logger.info(
            f"Trade APPROVED: {setup.pair} {setup.direction} | "
            f"size={position_size:.6f} leverage={leverage:.2f}x "
            f"notional=${notional:.2f} ({risk_pct*100:.0f}% of ${capital:.0f})"
        )

        return RiskApproval(
            approved=True,
            position_size=position_size,
            leverage=leverage,
            risk_pct=risk_pct,
            reason="All checks passed",
        )

    # ================================================================
    # Trade lifecycle (for Execution Service)
    # ================================================================

    def on_trade_opened(
        self, pair: str, direction: str, entry_price: float, timestamp: int
    ) -> None:
        """Notify Risk Service that a trade was opened."""
        self._state.record_trade_opened(pair, direction, entry_price, timestamp)

    def on_trade_closed(
        self, pair: str, direction: str, pnl_pct: float, timestamp: int
    ) -> None:
        """Notify Risk Service that a trade was closed."""
        self._state.record_trade_closed(pair, direction, pnl_pct, timestamp)

    def on_trade_cancelled(self, pair: str, direction: str) -> None:
        """Notify Risk Service that a pending entry was cancelled (never filled).

        Removes from open positions count without counting as a trade
        or affecting P&L tracking.
        """
        self._state.record_trade_cancelled(pair, direction)

    def update_capital(self, amount: float) -> None:
        """Update tracked capital (e.g. from exchange balance query)."""
        self._state.set_capital(amount)

    def _persist_risk_event(self, event_type: str, details: dict) -> None:
        """Write risk event to PostgreSQL (fire-and-forget)."""
        if self._data_service is None:
            return
        try:
            self._data_service.postgres.insert_risk_event(event_type, details)
        except Exception as e:
            self._persist_failures += 1
            logger.error(f"Failed to persist risk event: {e}")
            if self._persist_failures > 5:
                logger.warning(
                    f"Risk event persistence failed {self._persist_failures} times — "
                    f"check PostgreSQL connection"
                )
