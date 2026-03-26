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
        redis_store = data_service.redis if data_service is not None else None
        self._state = RiskStateTracker(capital, redis_store=redis_store)
        self._data_service = data_service
        self._persist_failures: int = 0
        logger.info(f"Risk Service initialized with capital=${capital:.2f}")

    # ================================================================
    # Main entry point
    # ================================================================

    def check(self, setup: TradeSetup, ai_confidence: float = 1.0, dry_run: bool = False) -> RiskApproval:
        """Run all guardrails and calculate position size.

        Fails fast — first guardrail failure rejects the trade.

        Args:
            ai_confidence: AI filter confidence (0.0-1.0). Used for
                confidence-based bet sizing when BET_SIZING_ENABLED=true.
                Default 1.0 (full size) for bypassed setups.
            dry_run: If True, skip balance fetch, capital mutation, and
                risk event persistence. Used by shadow mode to evaluate
                risk without side effects on the live pipeline.
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
                if not dry_run:
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
        if dry_run:
            # Shadow mode: use cached capital, no exchange call, no state mutation
            capital = self._state.get_capital()
        else:
            # Live: try exchange balance, fall back to tracked
            capital = self._query_account_balance()
            if capital is None:
                capital = self._state.get_capital()
            else:
                self._state.set_capital(capital)

        risk_pct = settings.RISK_PER_TRADE
        sl_distance = abs(setup.entry_price - setup.sl_price)

        # Dynamic sizing: size = (capital * risk_pct) / sl_distance
        # Leverage derived from resulting notional. Falls back to flat margin
        # if PositionSizer raises (should not happen — guardrails already checked).
        try:
            position_size, leverage = self._sizer.calculate(
                entry=setup.entry_price,
                sl=setup.sl_price,
                capital=capital,
                risk_pct=risk_pct,
            )
            risk_amount = capital * risk_pct
            notional = position_size * setup.entry_price
            # In isolated margin mode, margin = notional / leverage.
            # For risk-based sizing, the actual $ at risk is already capped
            # by risk_pct × capital regardless of margin requirements.
            margin = notional / leverage if leverage > 1.0 else notional
        except ValueError:
            # Fallback to flat margin (entry==sl already caught by guardrails,
            # but defensive in case of floating point edge cases)
            logger.warning(
                f"PositionSizer failed for {setup.pair} — "
                f"falling back to FIXED_TRADE_MARGIN=${settings.FIXED_TRADE_MARGIN}"
            )
            leverage = float(settings.MAX_LEVERAGE)
            margin = settings.FIXED_TRADE_MARGIN
            notional = margin * leverage
            position_size = notional / setup.entry_price
            risk_amount = margin

        # Confidence-based bet sizing (López de Prado, AFML Ch.10).
        # Half-Kelly: factor = KELLY_FRACTION × (2p - 1) where p = confidence.
        # Scales position size proportionally to conviction.
        # Only active when BET_SIZING_ENABLED and confidence < 1.0 (real AI score).
        if settings.BET_SIZING_ENABLED and ai_confidence < 1.0:
            raw_factor = settings.KELLY_FRACTION * (2 * ai_confidence - 1)
            bet_factor = max(settings.BET_SIZE_MIN, min(raw_factor, settings.BET_SIZE_MAX))
            position_size *= bet_factor
            notional = position_size * setup.entry_price
            margin = notional / leverage if leverage > 1.0 else notional
            risk_pct *= bet_factor
            logger.info(
                f"Bet sizing: confidence={ai_confidence:.2f} "
                f"kelly_raw={raw_factor:.3f} factor={bet_factor:.3f} "
                f"margin=${margin:.2f}"
            )

        # Hard cap: risk amount must not exceed MAX_MARGIN_PCT of capital (AFML Ch.10 —
        # even Half-Kelly can over-bet when BET_SIZE_MAX > 1.0; this prevents a
        # single position from risking more than the guardrail-intended fraction).
        # With PositionSizer, the actual $ at risk = risk_pct × capital, which is
        # already bounded by RISK_PER_TRADE. This cap only bites when bet sizing
        # pushes risk_pct above the limit.
        max_risk = capital * settings.MAX_MARGIN_PCT_OF_CAPITAL
        actual_risk = capital * risk_pct
        if capital > 0 and actual_risk > max_risk:
            logger.warning(
                f"Risk ${actual_risk:.2f} exceeds {settings.MAX_MARGIN_PCT_OF_CAPITAL*100:.0f}% "
                f"of capital ${capital:.2f} — capping to ${max_risk:.2f}"
            )
            risk_pct = settings.MAX_MARGIN_PCT_OF_CAPITAL
            # Re-run sizer with capped risk
            try:
                position_size, leverage = self._sizer.calculate(
                    entry=setup.entry_price,
                    sl=setup.sl_price,
                    capital=capital,
                    risk_pct=risk_pct,
                )
                notional = position_size * setup.entry_price
                margin = notional / leverage if leverage > 1.0 else notional
            except ValueError:
                pass  # Should not happen — already passed guardrails

        logger.info(
            f"Position sizing [{setup.pair}]: balance=${capital:.2f} "
            f"risk_pct={risk_pct*100:.1f}% risk_amount=${capital*risk_pct:.2f} "
            f"sl_distance={sl_distance:.2f} ({sl_distance/setup.entry_price*100:.2f}%) "
            f"size={position_size:.6f} leverage={leverage:.1f}x margin=${margin:.2f}"
        )

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

        # --- Portfolio heat check (after sizing — needs position_size) ---
        new_trade_heat = position_size * sl_distance
        current_heat = self._state.get_portfolio_heat_usd()
        heat_passed, heat_reason = self._guardrails.check_portfolio_heat(
            current_heat, new_trade_heat, capital
        )
        if not heat_passed:
            logger.warning(f"Trade REJECTED: {heat_reason} | {setup.pair} {setup.direction}")
            if not dry_run:
                self._persist_risk_event("guardrail_rejected", {
                    "pair": setup.pair,
                    "direction": setup.direction,
                    "reason": heat_reason,
                })
            return RiskApproval(
                approved=False,
                position_size=0.0,
                leverage=0.0,
                risk_pct=0.0,
                reason=heat_reason,
            )

        logger.info(
            f"Trade APPROVED: {setup.pair} {setup.direction} | "
            f"size={position_size:.6f} leverage={leverage:.1f}x "
            f"margin=${margin:.2f} notional=${notional:.2f} "
            f"risk_pct={risk_pct*100:.1f}% "
            f"heat=${current_heat + new_trade_heat:.2f}/{capital * settings.MAX_PORTFOLIO_HEAT_PCT:.2f}"
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
        self, pair: str, direction: str, entry_price: float, timestamp: int,
        *, phase: str = "pending",
        sl_price: float = 0.0,
        position_size: float = 0.0,
    ) -> None:
        """Notify Risk Service that a trade was opened.

        Args:
            phase: "pending" for limit orders, "active" for already-filled.
            sl_price: Stop-loss price for portfolio heat tracking.
            position_size: Position size in base currency for portfolio heat tracking.
        """
        self._state.record_trade_opened(
            pair, direction, entry_price, timestamp,
            phase=phase, sl_price=sl_price, position_size=position_size,
        )

    def on_trade_filled(self, pair: str, direction: str) -> None:
        """Notify Risk Service that a pending entry was filled (now active)."""
        self._state.record_trade_filled(pair, direction)

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

    def _query_account_balance(self) -> float | None:
        """Query live USDT balance from OKX. Returns None on failure."""
        if self._data_service is None:
            return None
        try:
            exchange = getattr(self._data_service, 'exchange', None)
            if exchange is None:
                return None
            balance = exchange.fetch_usdt_balance()
            if balance is not None and balance > 0:
                return balance
            return None
        except Exception as e:
            logger.warning(f"Balance query failed — using tracked capital: {e}")
            return None

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
