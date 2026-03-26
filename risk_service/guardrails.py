"""
Pure guardrail checks — each returns (passed, reason).

No state. Each method takes the values it needs and returns a verdict.
All thresholds come from config.settings.
"""

from config.settings import settings, QUICK_SETUP_TYPES
from shared.models import TradeSetup


class Guardrails:
    """Non-negotiable risk checks. If any fails, the trade does NOT execute."""

    def check_min_risk_distance(self, setup: TradeSetup) -> tuple[bool, str]:
        """Check that SL distance is at least MIN_RISK_DISTANCE_PCT of entry.

        Tiny risk distances produce noise trades where commissions eat
        the profit (e.g. $2.91 SL on $1975 = 0.15% → TP1 profit ≈ $0.07).
        """
        if setup.entry_price == 0:
            return False, "Entry price is zero"
        risk_pct = abs(setup.entry_price - setup.sl_price) / setup.entry_price
        min_pct = settings.MIN_RISK_DISTANCE_PCT
        if risk_pct < min_pct:
            return False, (
                f"Risk distance {risk_pct*100:.2f}% below minimum "
                f"{min_pct*100:.1f}% (SL too close to entry)"
            )
        return True, f"Risk distance {risk_pct*100:.2f}% OK"

    def check_rr_ratio(self, setup: TradeSetup) -> tuple[bool, str]:
        """Check that TP2 reward/risk >= minimum R:R.

        Uses MIN_RISK_REWARD_QUICK (1.5) for quick setups (C/D/E),
        MIN_RISK_REWARD (2.0) for swing setups (A/B/F/G).
        """
        risk = abs(setup.entry_price - setup.sl_price)
        if risk == 0:
            return False, "Risk is zero (entry == SL)"

        reward = abs(setup.tp2_price - setup.entry_price)
        rr = reward / risk

        min_rr = (settings.MIN_RISK_REWARD_QUICK
                  if setup.setup_type in QUICK_SETUP_TYPES
                  else settings.MIN_RISK_REWARD)

        if rr < min_rr:
            return False, f"R:R {rr:.2f} below minimum {min_rr}"
        return True, f"R:R {rr:.2f} OK"

    def check_cooldown(
        self, last_loss_time: int | None, current_time: int
    ) -> tuple[bool, str]:
        """Check that COOLDOWN_MINUTES have elapsed since last loss.

        Args:
            last_loss_time: Unix timestamp (seconds) of last loss, or None.
            current_time: Current Unix timestamp (seconds).
        """
        if last_loss_time is None:
            return True, "No recent loss"

        elapsed_min = (current_time - last_loss_time) / 60
        if elapsed_min < settings.COOLDOWN_MINUTES:
            remaining = settings.COOLDOWN_MINUTES - elapsed_min
            return False, f"Cooldown active, {remaining:.0f} min remaining"
        return True, "Cooldown elapsed"

    def check_max_trades_today(self, count: int) -> tuple[bool, str]:
        """Check that trades today < MAX_TRADES_PER_DAY."""
        if count >= settings.MAX_TRADES_PER_DAY:
            return False, f"Max trades/day reached ({count}/{settings.MAX_TRADES_PER_DAY})"
        return True, f"Trades today {count}/{settings.MAX_TRADES_PER_DAY}"

    def check_max_open_positions(self, count: int) -> tuple[bool, str]:
        """Check that open positions < MAX_OPEN_POSITIONS."""
        if count >= settings.MAX_OPEN_POSITIONS:
            return False, f"Max open positions reached ({count}/{settings.MAX_OPEN_POSITIONS})"
        return True, f"Open positions {count}/{settings.MAX_OPEN_POSITIONS}"

    def check_daily_drawdown(self, dd_pct: float) -> tuple[bool, str]:
        """Check that daily drawdown < MAX_DAILY_DRAWDOWN.

        Args:
            dd_pct: Current daily drawdown as positive fraction (e.g. 0.02 = 2%).
        """
        if dd_pct >= settings.MAX_DAILY_DRAWDOWN:
            return False, f"Daily DD {dd_pct*100:.1f}% >= limit {settings.MAX_DAILY_DRAWDOWN*100:.1f}%"
        return True, f"Daily DD {dd_pct*100:.1f}% OK"

    def check_weekly_drawdown(self, dd_pct: float) -> tuple[bool, str]:
        """Check that weekly drawdown < MAX_WEEKLY_DRAWDOWN.

        Args:
            dd_pct: Current weekly drawdown as positive fraction (e.g. 0.04 = 4%).
        """
        if dd_pct >= settings.MAX_WEEKLY_DRAWDOWN:
            return False, f"Weekly DD {dd_pct*100:.1f}% >= limit {settings.MAX_WEEKLY_DRAWDOWN*100:.1f}%"
        return True, f"Weekly DD {dd_pct*100:.1f}% OK"

    def check_portfolio_heat(
        self,
        current_heat_usd: float,
        new_trade_heat_usd: float,
        capital: float,
    ) -> tuple[bool, str]:
        """Check that total portfolio heat (existing + new trade) doesn't exceed limit.

        Portfolio heat = sum of (position_size × |entry - SL|) for all open positions.
        If total exceeds MAX_PORTFOLIO_HEAT_PCT of capital, reject.

        Args:
            current_heat_usd: Sum of $ at risk across existing open positions.
            new_trade_heat_usd: $ at risk for the proposed new trade.
            capital: Current account capital.
        """
        if capital <= 0:
            return False, "Capital is zero — cannot evaluate portfolio heat"

        total_heat = current_heat_usd + new_trade_heat_usd
        heat_pct = total_heat / capital
        max_pct = settings.MAX_PORTFOLIO_HEAT_PCT

        if heat_pct > max_pct:
            return False, (
                f"Portfolio heat ${total_heat:.2f} ({heat_pct*100:.1f}%) "
                f"exceeds {max_pct*100:.0f}% of capital ${capital:.2f} "
                f"(existing=${current_heat_usd:.2f} + new=${new_trade_heat_usd:.2f})"
            )
        return True, f"Portfolio heat {heat_pct*100:.1f}% OK"
