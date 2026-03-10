"""Tests for risk_service.service — full check() integration scenarios."""

import time
import pytest
from risk_service import RiskService
from shared.models import TradeSetup
from config.settings import settings


def _make_setup(
    entry=50000.0,
    sl=49000.0,
    tp1=51000.0,
    tp2=52000.0,
    direction="long",
    pair="BTC/USDT",
) -> TradeSetup:
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair,
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


@pytest.fixture
def risk(monkeypatch):
    monkeypatch.setattr(settings, "OKX_SANDBOX", False)
    monkeypatch.setattr(settings, "TRADE_CAPITAL_PCT", 0.15)
    # Capital high enough for BTC min order (0.01 BTC = $500 notional needs $3334 capital at 15%)
    return RiskService(capital=5000.0)


# ============================================================
# Happy path — all checks pass
# ============================================================

class TestApproval:

    def test_basic_approval(self, risk):
        """Standard setup with good R:R, no prior trades."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)

        assert result.approved is True
        assert result.position_size > 0
        assert result.leverage > 0
        assert result.risk_pct == settings.TRADE_CAPITAL_PCT
        assert result.reason == "All checks passed"

    def test_short_approval(self, risk):
        """Short setup should also work."""
        setup = _make_setup(
            entry=50000, sl=51000, tp2=48000, direction="short"
        )
        result = risk.check(setup)
        assert result.approved is True

    def test_position_size_correct(self, risk):
        """Verify calculated position size matches formula."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)

        # capital=5000, TRADE_CAPITAL_PCT=0.15 → notional=$750
        # position_size = $750 / $50000 = 0.015 BTC
        # leverage = MAX_LEVERAGE = 5
        assert result.position_size == pytest.approx(0.015, rel=1e-6)
        assert result.leverage == pytest.approx(5.0, rel=1e-6)


# ============================================================
# Rejections — each guardrail
# ============================================================

class TestRejections:

    def test_bad_rr_rejected(self, risk):
        """R:R < 1.5 should reject."""
        setup = _make_setup(entry=50000, sl=49000, tp2=51000)
        result = risk.check(setup)
        assert result.approved is False
        assert "R:R" in result.reason

    def test_cooldown_rejected(self, risk):
        """Trade during cooldown should reject."""
        now = int(time.time())
        risk.on_trade_opened("BTC/USDT", "long", 50000, now - 3600)
        risk.on_trade_closed("BTC/USDT", "long",-0.01, now - 600)  # Loss 10 min ago

        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)
        assert result.approved is False
        assert "Cooldown" in result.reason

    def test_max_trades_per_day_rejected(self, risk):
        """Exceeding daily trade limit should reject."""
        now = int(time.time())
        for i in range(settings.MAX_TRADES_PER_DAY):
            risk.on_trade_opened(f"BTC/USDT", "long", 50000, now + i)
            risk.on_trade_closed(f"BTC/USDT", "long", 0.001, now + i + 1)

        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)
        assert result.approved is False
        assert "trades/day" in result.reason

    def test_max_open_positions_rejected(self, risk):
        """Too many open positions should reject."""
        now = int(time.time())
        for i in range(settings.MAX_OPEN_POSITIONS):
            risk.on_trade_opened(f"PAIR{i}/USDT", "long", 50000, now + i)

        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)
        assert result.approved is False
        assert "open positions" in result.reason

    def test_daily_dd_rejected(self, risk):
        """Daily drawdown at limit should reject."""
        now = int(time.time())
        # Loss far enough ago that cooldown has elapsed, but DD persists same day
        past = now - (settings.COOLDOWN_MINUTES * 60 + 60)
        risk.on_trade_closed("BTC/USDT", "long",-settings.MAX_DAILY_DRAWDOWN, past)

        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)
        assert result.approved is False
        assert "Daily DD" in result.reason

    def test_weekly_dd_rejected(self, risk):
        """Weekly drawdown at limit should reject."""
        now = int(time.time())
        past = now - (settings.COOLDOWN_MINUTES * 60 + 60)
        risk.on_trade_closed("BTC/USDT", "long",-settings.MAX_WEEKLY_DRAWDOWN, past)

        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)
        assert result.approved is False
        # Could be daily or weekly DD since both thresholds may be hit
        assert "DD" in result.reason


# ============================================================
# Trade lifecycle integration
# ============================================================

class TestLifecycle:

    def test_trade_open_close_cycle(self, risk):
        """Full open → close cycle updates state correctly."""
        now = int(time.time())
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)

        # First trade approved
        result1 = risk.check(setup)
        assert result1.approved is True

        # Simulate opening
        risk.on_trade_opened("BTC/USDT", "long", 50000, now)

        # Close with profit
        risk.on_trade_closed("BTC/USDT", "long",0.02, now + 3600)

        # Second trade should also be approved
        result2 = risk.check(setup)
        assert result2.approved is True

    def test_capital_update(self, risk):
        """Capital update affects position sizing."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)

        result1 = risk.check(setup)
        risk.update_capital(10000.0)  # double from 5000
        result2 = risk.check(setup)

        assert result2.position_size == pytest.approx(
            result1.position_size * 2, rel=1e-6
        )

    def test_leverage_capped_in_check(self, risk):
        """Tight SL produces capped leverage through check()."""
        setup = _make_setup(
            entry=50000, sl=49750, tp2=52000
        )
        result = risk.check(setup)
        assert result.approved is True
        assert result.leverage <= settings.MAX_LEVERAGE

    def test_entry_equals_sl_rejected(self, risk):
        """Entry == SL should be caught as position sizing error.

        Note (M-R4): The ValueError path in position_sizer.calculate() is
        effectively unreachable because guardrails.check_rr_ratio() catches
        malformed setups (zero risk distance, inverted SL) before the sizer
        is called. This is by design — guardrails are the first line of defense.
        """
        setup = _make_setup(entry=50000, sl=50000, tp2=52000)
        result = risk.check(setup)
        # Rejected by R:R check (zero risk) before reaching position sizer
        assert result.approved is False
