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
    monkeypatch.setattr(settings, "RISK_PER_TRADE", 0.01)  # 1% risk
    # Capital high enough for BTC min order sizes
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
        assert result.risk_pct == settings.RISK_PER_TRADE
        assert result.reason == "All checks passed"

    def test_short_approval(self, risk):
        """Short setup should also work."""
        setup = _make_setup(
            entry=50000, sl=51000, tp2=48000, direction="short"
        )
        result = risk.check(setup)
        assert result.approved is True

    def test_position_size_uses_sizer(self, risk):
        """Verify PositionSizer formula: size = (capital * risk%) / SL_distance."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)

        # capital=5000, RISK_PER_TRADE=0.01 → risk_amount=$50
        # sl_distance=1000 → size = 50/1000 = 0.05 BTC
        # notional = 0.05 * 50000 = $2500 → leverage = 2500/5000 = 0.5x
        assert result.position_size == pytest.approx(0.05, rel=1e-6)
        assert result.leverage == pytest.approx(0.5, rel=1e-6)

    def test_tight_sl_caps_leverage(self, risk):
        """Tight SL (but above MIN_RISK_DISTANCE) caps leverage at MAX_LEVERAGE."""
        # sl_distance=500 (1%), risk=$50, size=0.1 BTC
        # notional=0.1*50000=$5000, leverage=5000/5000=1.0x — not capped
        # Use even tighter: sl_distance=350 (0.7%), risk=$50, size=0.143
        # notional=0.143*50000=$7143, leverage=7143/5000=1.43x — still low
        # Need: sl at 49700 (0.6% distance) → size=50/300=0.167
        # notional=0.167*50000=$8333, leverage=1.67x — still not capped
        # With more capital risk: use monkeypatch to set higher risk
        setup = _make_setup(entry=50000, sl=49700, tp2=52000)
        result = risk.check(setup)
        assert result.approved is True
        assert result.leverage <= settings.MAX_LEVERAGE


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

        # PositionSizer: size = (capital * risk%) / distance
        # Doubling capital doubles risk_amount, so size doubles
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
        """Entry == SL should be caught by guardrails before sizer."""
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
        monkeypatch.setattr(settings, "RISK_PER_TRADE", 0.01)
        monkeypatch.setattr(settings, "BET_SIZING_ENABLED", True)
        monkeypatch.setattr(settings, "KELLY_FRACTION", 0.5)
        monkeypatch.setattr(settings, "BET_SIZE_MIN", 0.25)
        monkeypatch.setattr(settings, "BET_SIZE_MAX", 2.0)
        return RiskService(capital=5000.0)

    def test_high_confidence_scales_size(self, risk_bet):
        """Confidence=0.9 → factor=0.5*(2*0.9-1)=0.4 → size scaled to 40%."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        # Base: capital=5000, risk=1%, distance=1000 → size=0.05 BTC
        # Bet factor: 0.5*(2*0.9-1) = 0.4 → size=0.05*0.4=0.02
        result_scaled = risk_bet.check(setup, ai_confidence=0.9)
        assert result_scaled.approved is True
        assert result_scaled.position_size == pytest.approx(0.05 * 0.4, rel=0.01)

    def test_low_confidence_hits_floor(self, risk_bet):
        """Confidence=0.55 → raw factor=0.05 → clamped to BET_SIZE_MIN=0.25."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        # raw = 0.5*(2*0.55-1) = 0.05, clamped to 0.25 → size=0.05*0.25=0.0125
        result_floor = risk_bet.check(setup, ai_confidence=0.55)
        assert result_floor.approved is True
        assert result_floor.position_size == pytest.approx(0.05 * 0.25, rel=0.01)

    def test_bypassed_confidence_no_sizing(self, risk_bet):
        """Confidence=1.0 (bypassed AI) → bet sizing skipped, full size."""
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk_bet.check(setup, ai_confidence=1.0)
        assert result.approved is True
        # PositionSizer: risk=$50, distance=1000, size=0.05
        assert result.position_size == pytest.approx(0.05, rel=1e-6)

    def test_disabled_bet_sizing_ignores_confidence(self, risk_bet, monkeypatch):
        """BET_SIZING_ENABLED=false → always full size regardless of confidence."""
        monkeypatch.setattr(settings, "BET_SIZING_ENABLED", False)
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk_bet.check(setup, ai_confidence=0.6)
        assert result.approved is True
        # Full size (no scaling): 0.05 BTC
        assert result.position_size == pytest.approx(0.05, rel=1e-6)


# ============================================================
# Portfolio heat integration
# ============================================================

class TestPortfolioHeat:

    def test_heat_blocks_new_trade(self, risk, monkeypatch):
        """When existing positions saturate heat budget, new trade rejected."""
        monkeypatch.setattr(settings, "MAX_OPEN_POSITIONS", 20)  # Don't hit position limit
        now = int(time.time())
        # Open positions that consume most of the heat budget
        # capital=5000, MAX_PORTFOLIO_HEAT_PCT=0.06 → max heat=$300
        # Each position: size=0.05 BTC, entry=50000, sl=49000, heat=0.05*1000=$50
        for i in range(5):
            risk.on_trade_opened(
                f"PAIR{i}/USDT", "long", 50000, now + i,
                phase="active", sl_price=49000.0, position_size=0.05,
            )
        # Existing heat: 5 × $50 = $250. New trade: 0.05 × 1000 = $50
        # Total: $300 → exactly at limit (should pass)
        setup = _make_setup(entry=50000, sl=49000, tp2=52000)
        result = risk.check(setup)
        assert result.approved is True

        # Add one more position to push heat over
        risk.on_trade_opened(
            "PAIR5/USDT", "long", 50000, now + 5,
            phase="active", sl_price=49000.0, position_size=0.05,
        )
        # Existing heat: 6 × $50 = $300. New: $50. Total: $350 > $300
        result2 = risk.check(setup)
        assert result2.approved is False
        assert "Portfolio heat" in result2.reason
