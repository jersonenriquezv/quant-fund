"""Tests for risk_service.guardrails — each rule check pass/fail."""

import time
import pytest
from risk_service.guardrails import Guardrails
from shared.models import TradeSetup
from config.settings import settings


@pytest.fixture
def g():
    return Guardrails()


def _make_setup(
    entry=50000.0,
    sl=49000.0,
    tp1=51000.0,
    tp2=52000.0,
    direction="long",
) -> TradeSetup:
    """Create a TradeSetup with controllable prices."""
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair="BTC/USDT",
        direction=direction,
        setup_type="setup_a",
        entry_price=entry,
        sl_price=sl,
        tp1_price=tp1,
        tp2_price=tp2,
        confluences=["choch", "ob", "sweep"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
    )


# ============================================================
# R:R ratio
# ============================================================

class TestRRRatio:

    def test_good_rr_passes(self, g):
        """TP2=52000, entry=50000, SL=49000 → R:R = 2.0 >= 2.0."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        passed, reason = g.check_rr_ratio(setup)
        assert passed is True

    def test_exact_minimum_passes(self, g):
        """R:R = exactly 2.0 should pass."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        passed, _ = g.check_rr_ratio(setup)
        assert passed is True

    def test_below_minimum_fails(self, g):
        """R:R = 1.5 < 2.0 should fail."""
        setup = _make_setup(entry=50000, sl=49000, tp2=51500)
        passed, reason = g.check_rr_ratio(setup)
        assert passed is False
        assert "below minimum" in reason

    def test_short_direction_rr(self, g):
        """Short: entry=50000, SL=51000, TP2=48000 → R:R = 2.0."""
        setup = _make_setup(
            entry=50000, sl=51000, tp2=48000, direction="short"
        )
        passed, _ = g.check_rr_ratio(setup)
        assert passed is True

    def test_zero_risk_fails(self, g):
        """Entry == SL → risk is zero → reject."""
        setup = _make_setup(entry=50000, sl=50000, tp2=52000)
        passed, reason = g.check_rr_ratio(setup)
        assert passed is False
        assert "zero" in reason.lower()


# ============================================================
# Cooldown
# ============================================================

class TestCooldown:

    def test_no_previous_loss(self, g):
        """No last loss → always pass."""
        passed, _ = g.check_cooldown(None, int(time.time()))
        assert passed is True

    def test_cooldown_not_elapsed(self, g):
        """Loss 10 min ago, cooldown is 30 min → fail."""
        now = int(time.time())
        last_loss = now - 600  # 10 min ago
        passed, reason = g.check_cooldown(last_loss, now)
        assert passed is False
        assert "remaining" in reason

    def test_cooldown_elapsed(self, g):
        """Loss 31 min ago → pass."""
        now = int(time.time())
        last_loss = now - (settings.COOLDOWN_MINUTES * 60 + 60)
        passed, _ = g.check_cooldown(last_loss, now)
        assert passed is True


# ============================================================
# Max trades per day
# ============================================================

class TestMaxTradesPerDay:

    def test_below_limit(self, g):
        passed, _ = g.check_max_trades_today(0)
        assert passed is True

    def test_at_limit(self, g):
        passed, _ = g.check_max_trades_today(settings.MAX_TRADES_PER_DAY)
        assert passed is False

    def test_above_limit(self, g):
        passed, _ = g.check_max_trades_today(settings.MAX_TRADES_PER_DAY + 1)
        assert passed is False


# ============================================================
# Max open positions
# ============================================================

class TestMaxOpenPositions:

    def test_below_limit(self, g):
        passed, _ = g.check_max_open_positions(0)
        assert passed is True

    def test_at_limit(self, g):
        passed, _ = g.check_max_open_positions(settings.MAX_OPEN_POSITIONS)
        assert passed is False


# ============================================================
# Drawdown checks
# ============================================================

class TestDrawdown:

    def test_daily_dd_below_limit(self, g):
        passed, _ = g.check_daily_drawdown(0.01)
        assert passed is True

    def test_daily_dd_at_limit(self, g):
        passed, _ = g.check_daily_drawdown(settings.MAX_DAILY_DRAWDOWN)
        assert passed is False

    def test_daily_dd_above_limit(self, g):
        passed, _ = g.check_daily_drawdown(0.05)
        assert passed is False

    def test_daily_dd_zero(self, g):
        passed, _ = g.check_daily_drawdown(0.0)
        assert passed is True

    def test_weekly_dd_below_limit(self, g):
        passed, _ = g.check_weekly_drawdown(0.02)
        assert passed is True

    def test_weekly_dd_at_limit(self, g):
        passed, _ = g.check_weekly_drawdown(settings.MAX_WEEKLY_DRAWDOWN)
        assert passed is False

    def test_weekly_dd_above_limit(self, g):
        passed, _ = g.check_weekly_drawdown(0.10)
        assert passed is False


# ============================================================
# Portfolio heat
# ============================================================

class TestPortfolioHeat:

    def test_no_existing_heat_passes(self, g):
        """New trade with no open positions should pass."""
        passed, _ = g.check_portfolio_heat(
            current_heat_usd=0.0, new_trade_heat_usd=50.0, capital=1000.0
        )
        assert passed is True

    def test_heat_within_limit_passes(self, g):
        """Existing $40 + new $15 = $55 < 6% of $1000 = $60."""
        passed, _ = g.check_portfolio_heat(
            current_heat_usd=40.0, new_trade_heat_usd=15.0, capital=1000.0
        )
        assert passed is True

    def test_heat_exceeds_limit_rejected(self, g):
        """Existing $50 + new $15 = $65 > 6% of $1000 = $60."""
        passed, reason = g.check_portfolio_heat(
            current_heat_usd=50.0, new_trade_heat_usd=15.0, capital=1000.0
        )
        assert passed is False
        assert "Portfolio heat" in reason
        assert "exceeds" in reason

    def test_heat_at_exact_limit_passes(self, g):
        """Exactly at 6% should pass (not strictly greater)."""
        passed, _ = g.check_portfolio_heat(
            current_heat_usd=50.0, new_trade_heat_usd=10.0, capital=1000.0
        )
        assert passed is True
