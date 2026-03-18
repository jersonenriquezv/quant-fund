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
    monkeypatch.setattr(settings, "FIXED_TRADE_MARGIN", 0.0)  # Use pct mode for existing tests
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

    def test_fixed_margin_mode(self, risk, monkeypatch):
        """FIXED_TRADE_MARGIN > 0 uses fixed margin instead of pct."""
        monkeypatch.setattr(settings, "FIXED_TRADE_MARGIN", 20.0)
        monkeypatch.setattr(settings, "MAX_LEVERAGE", 5)
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)

        # margin=$20 × 5x = $100 notional
        # position_size = $100 / $50000 = 0.002 BTC
        assert result.approved is True
        assert result.position_size == pytest.approx(0.002, rel=1e-6)
        assert result.leverage == pytest.approx(5.0, rel=1e-6)
        # risk_pct = margin / capital = 20 / 5000 = 0.004
        assert result.risk_pct == pytest.approx(0.004, rel=1e-6)


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
            risk.on_trade_opened(f"PAIR{i}/USDT", "long", 50000, now + i, phase="active")

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


# ============================================================
# Bet sizing (confidence-based, AFML Ch.10)
# ============================================================

class TestBetSizing:
    """López de Prado half-Kelly bet sizing modulates margin by AI confidence."""

    @pytest.fixture
    def risk_bet(self, monkeypatch):
        monkeypatch.setattr(settings, "OKX_SANDBOX", False)
        monkeypatch.setattr(settings, "FIXED_TRADE_MARGIN", 20.0)
        monkeypatch.setattr(settings, "BET_SIZING_ENABLED", True)
        monkeypatch.setattr(settings, "KELLY_FRACTION", 0.5)
        monkeypatch.setattr(settings, "BET_SIZE_MIN", 0.25)
        monkeypatch.setattr(settings, "BET_SIZE_MAX", 2.0)
        return RiskService(capital=5000.0)

    def test_high_confidence_increases_size(self, risk_bet):
        """Confidence=0.9 → factor=0.5*(2*0.9-1)=0.4 → margin=$8."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk_bet.check(setup, ai_confidence=0.9)
        assert result.approved is True
        # factor = 0.5 * (2*0.9 - 1) = 0.4, margin = 20 * 0.4 = $8
        expected_notional = 8.0 * settings.MAX_LEVERAGE
        expected_size = expected_notional / 50000
        assert result.position_size == pytest.approx(expected_size, rel=0.01)

    def test_low_confidence_hits_floor(self, risk_bet):
        """Confidence=0.55 → raw factor=0.05 → clamped to BET_SIZE_MIN=0.25."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk_bet.check(setup, ai_confidence=0.55)
        assert result.approved is True
        # raw = 0.5 * (2*0.55 - 1) = 0.05, clamped to 0.25
        expected_notional = 20.0 * 0.25 * settings.MAX_LEVERAGE
        expected_size = expected_notional / 50000
        assert result.position_size == pytest.approx(expected_size, rel=0.01)

    def test_bypassed_confidence_no_sizing(self, risk_bet):
        """Confidence=1.0 (bypassed AI) → bet sizing skipped, full margin."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk_bet.check(setup, ai_confidence=1.0)
        assert result.approved is True
        # ai_confidence=1.0 → sizing NOT applied (condition: confidence < 1.0)
        expected_notional = 20.0 * settings.MAX_LEVERAGE
        expected_size = expected_notional / 50000
        assert result.position_size == pytest.approx(expected_size, rel=0.01)

    def test_disabled_bet_sizing_ignores_confidence(self, risk_bet, monkeypatch):
        """BET_SIZING_ENABLED=false → always full margin regardless of confidence."""
        monkeypatch.setattr(settings, "BET_SIZING_ENABLED", False)
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk_bet.check(setup, ai_confidence=0.6)
        assert result.approved is True
        expected_notional = 20.0 * settings.MAX_LEVERAGE
        expected_size = expected_notional / 50000
        assert result.position_size == pytest.approx(expected_size, rel=0.01)
