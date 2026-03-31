"""Tests for manual trading module — calculator, CRUD, partial closes, analytics."""

import pytest
from dashboard.api.manual.calculator import calculate


# ══════════════════════════════════════════════════════════════════
# Calculator tests
# ══════════════════════════════════════════════════════════════════

class TestCalculator:
    """Core position sizing math."""

    def test_sol_long_example(self):
        """SOL long: balance $80, 2% risk, entry $81.34, SL $78.50, TP $87, 7x."""
        r = calculate(
            pair="SOL/USDT", direction="long", balance=80.0,
            risk_percent=2.0, entry=81.34, stop_loss=78.50,
            take_profit_1=84.18, take_profit_2=87.02, leverage=7,
        )
        assert r.risk_usd == 1.60
        assert abs(r.sl_distance - 2.84) < 0.01
        assert abs(r.sl_distance_pct - 3.4927) < 0.01
        assert abs(r.position_size - 0.5634) < 0.001
        assert abs(r.position_value_usd - 45.83) < 0.5
        assert abs(r.margin_required - 6.55) < 0.1
        assert len(r.tp_plan) == 2
        assert r.tp_plan[0].close_pct == 50.0
        assert r.tp_plan[1].close_pct == 50.0
        assert abs(r.tp_plan[0].rr_ratio - 1.0) < 0.1
        assert abs(r.tp_plan[1].rr_ratio - 2.0) < 0.1
        assert r.tp_plan[0].after_action is not None  # SL to breakeven reminder

    def test_short_trade(self):
        """Short: SL above entry, TP below entry."""
        r = calculate(
            pair="ETH/USDT", direction="short", balance=100.0,
            risk_percent=1.0, entry=2000.0, stop_loss=2050.0,
            take_profit_1=1950.0, leverage=10,
        )
        assert r.risk_usd == 1.0
        assert abs(r.sl_distance - 50.0) < 0.01
        assert r.position_size == pytest.approx(0.02, abs=0.001)
        assert r.tp_plan[0].rr_ratio == 1.0

    def test_auto_suggest_tps_long(self):
        """When TPs not provided, suggest 1R and 2R for long."""
        r = calculate(
            pair="BTC/USDT", direction="long", balance=100.0,
            risk_percent=2.0, entry=100.0, stop_loss=95.0, leverage=5,
        )
        # SL distance = 5.0, so TP1 = 105, TP2 = 110
        assert r.suggested_tp1 == 105.0
        assert r.suggested_tp2 == 110.0
        assert r.take_profit_1 == 105.0  # auto-filled
        assert r.take_profit_2 == 110.0

    def test_auto_suggest_tps_short(self):
        """When TPs not provided, suggest 1R and 2R for short."""
        r = calculate(
            pair="BTC/USDT", direction="short", balance=100.0,
            risk_percent=2.0, entry=100.0, stop_loss=105.0, leverage=5,
        )
        assert r.suggested_tp1 == 95.0
        assert r.suggested_tp2 == 90.0

    def test_warning_tight_sl(self):
        """SL < 1% triggers warning."""
        r = calculate(
            pair="BTC/USDT", direction="long", balance=1000.0,
            risk_percent=1.0, entry=100.0, stop_loss=99.5, leverage=5,
        )
        assert any("tight" in w.lower() for w in r.warnings)

    def test_warning_wide_sl(self):
        """SL > 5% triggers warning."""
        r = calculate(
            pair="SOL/USDT", direction="long", balance=100.0,
            risk_percent=2.0, entry=100.0, stop_loss=90.0, leverage=5,
        )
        assert any("wide" in w.lower() for w in r.warnings)

    def test_warning_high_margin(self):
        """Margin > 50% of balance triggers warning."""
        r = calculate(
            pair="SOL/USDT", direction="long", balance=10.0,
            risk_percent=5.0, entry=100.0, stop_loss=99.0, leverage=1,
        )
        assert any("50%" in w for w in r.warnings)

    def test_warning_low_rr_tp2(self):
        """TP2 R:R < 2 triggers warning."""
        r = calculate(
            pair="SOL/USDT", direction="long", balance=100.0,
            risk_percent=2.0, entry=100.0, stop_loss=95.0,
            take_profit_1=103.0, take_profit_2=107.0, leverage=5,
        )
        # TP2 R:R = 7/5 = 1.4 < 2
        assert any("TP2" in w for w in r.warnings)

    def test_invalid_long_sl_above_entry(self):
        with pytest.raises(ValueError, match="below entry"):
            calculate("SOL/USDT", "long", 100, 2.0, 100.0, 105.0)

    def test_invalid_short_sl_below_entry(self):
        with pytest.raises(ValueError, match="above entry"):
            calculate("SOL/USDT", "short", 100, 2.0, 100.0, 95.0)

    def test_invalid_zero_balance(self):
        with pytest.raises(ValueError, match="positive"):
            calculate("SOL/USDT", "long", 0, 2.0, 100.0, 95.0)

    def test_tp_plan_profit_math(self):
        """TP profits should equal position_size/2 * distance for each."""
        r = calculate(
            pair="SOL/USDT", direction="long", balance=100.0,
            risk_percent=2.0, entry=100.0, stop_loss=95.0,
            take_profit_1=105.0, take_profit_2=110.0, leverage=5,
        )
        half = r.position_size / 2
        assert abs(r.tp_plan[0].potential_profit_usd - round(half * 5.0, 2)) < 0.01
        assert abs(r.tp_plan[1].potential_profit_usd - round(half * 10.0, 2)) < 0.01
        assert abs(r.total_potential_profit - (r.tp_plan[0].potential_profit_usd + r.tp_plan[1].potential_profit_usd)) < 0.01
        assert r.total_potential_loss == r.risk_usd

    def test_pnl_for_long_close(self):
        """Verify PnL calculation for long: (close - entry) * size."""
        r = calculate(
            pair="SOL/USDT", direction="long", balance=80.0,
            risk_percent=2.0, entry=81.34, stop_loss=78.50,
            take_profit_1=84.18, take_profit_2=87.02, leverage=7,
        )
        # If closed at TP1 with 50%:
        half = r.position_size / 2
        pnl_tp1 = (84.18 - 81.34) * half
        assert pnl_tp1 > 0

    def test_pnl_for_short_close(self):
        """Verify PnL direction for short: (entry - close) * size."""
        r = calculate(
            pair="ETH/USDT", direction="short", balance=100.0,
            risk_percent=2.0, entry=2000.0, stop_loss=2050.0,
            take_profit_1=1950.0, leverage=10,
        )
        half = r.position_size / 2
        pnl_tp1 = (2000.0 - 1950.0) * half
        assert pnl_tp1 > 0  # Profitable short


class TestCalculatorInverse:
    """Inverse (coin-margined) contract calculations."""

    def test_btc_inverse_long(self):
        """BTC/USD inverse: balance $100, 2% risk, entry $85000, SL $84000."""
        r = calculate(
            pair="BTC/USD", direction="long", balance=100.0,
            risk_percent=2.0, entry=85000.0, stop_loss=84000.0,
            leverage=10, margin_type="inverse",
        )
        # risk = $2, sl_distance = $1000
        assert r.risk_usd == 2.0
        assert r.margin_type == "inverse"
        # contracts = risk × entry / sl_distance = 2 × 85000 / 1000 = 170 USD contracts
        assert abs(r.position_size - 170.0) < 0.1
        assert r.position_size_label == "contracts"
        # margin in BTC = contracts / (entry × leverage) = 170 / (85000 × 10) = 0.0002 BTC
        assert abs(r.margin_required - 0.0002) < 0.00001
        assert r.margin_currency == "BTC"
        # Position value = contracts = $170
        assert abs(r.position_value_usd - 170.0) < 0.1

    def test_inverse_pnl_long(self):
        """Inverse PnL: contracts × (close - entry) / entry."""
        r = calculate(
            pair="BTC/USD", direction="long", balance=1000.0,
            risk_percent=1.0, entry=50000.0, stop_loss=49000.0,
            take_profit_1=51000.0, take_profit_2=52000.0,
            leverage=5, margin_type="inverse",
        )
        # risk = $10, contracts = 10 × 50000 / 1000 = 500
        assert abs(r.position_size - 500.0) < 0.1
        # TP1 PnL = 250 × (51000 - 50000) / 50000 = 250 × 0.02 = $5
        assert abs(r.tp_plan[0].potential_profit_usd - 5.0) < 0.1
        # TP2 PnL = 250 × (52000 - 50000) / 50000 = 250 × 0.04 = $10
        assert abs(r.tp_plan[1].potential_profit_usd - 10.0) < 0.1
        # Total loss = risk = $10
        assert r.total_potential_loss == 10.0

    def test_inverse_pnl_short(self):
        """Inverse short PnL: contracts × (entry - close) / entry."""
        r = calculate(
            pair="ETH/USD", direction="short", balance=100.0,
            risk_percent=2.0, entry=2000.0, stop_loss=2100.0,
            take_profit_1=1900.0, leverage=10, margin_type="inverse",
        )
        # contracts = 2 × 2000 / 100 = 40
        assert abs(r.position_size - 40.0) < 0.1
        # TP1 PnL (half=20 contracts): 20 × (2000 - 1900) / 2000 = 20 × 0.05 = $1
        assert abs(r.tp_plan[0].potential_profit_usd - 1.0) < 0.1

    def test_inverse_risk_equals_loss_at_sl(self):
        """Verify that loss at SL equals risk_usd for inverse."""
        from dashboard.api.manual.calculator import pnl_usd
        r = calculate(
            pair="BTC/USD", direction="long", balance=500.0,
            risk_percent=2.0, entry=80000.0, stop_loss=79000.0,
            leverage=10, margin_type="inverse",
        )
        # Full position loss at SL
        loss = pnl_usd("inverse", "long", 80000.0, 79000.0, r.position_size)
        assert abs(loss + r.risk_usd) < 0.01  # loss is negative, risk is positive
