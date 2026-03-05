"""Tests for risk_service.position_sizer — formula, leverage cap, edge cases."""

import pytest
from unittest.mock import patch
from risk_service.position_sizer import PositionSizer
from config.settings import settings


@pytest.fixture
def sizer():
    return PositionSizer()


# ============================================================
# Basic formula
# ============================================================

class TestPositionSizerFormula:
    """Test the core position sizing formula."""

    def test_long_basic(self, sizer):
        """Long: entry=50000, sl=49000, capital=1000, risk=2%."""
        size, lev = sizer.calculate(
            entry=50000, sl=49000, capital=1000, risk_pct=0.02
        )
        # risk_amount = 1000 * 0.02 = 20
        # distance = 1000
        # size = 20 / 1000 = 0.02 BTC
        assert pytest.approx(size, rel=1e-6) == 0.02
        # leverage = (0.02 * 50000) / 1000 = 1.0
        assert pytest.approx(lev, rel=1e-6) == 1.0

    def test_short_basic(self, sizer):
        """Short: entry=50000, sl=51000, same result (abs distance)."""
        size, lev = sizer.calculate(
            entry=50000, sl=51000, capital=1000, risk_pct=0.02
        )
        assert pytest.approx(size, rel=1e-6) == 0.02
        assert pytest.approx(lev, rel=1e-6) == 1.0

    def test_tight_sl_higher_leverage(self, sizer):
        """Tight SL should require more leverage."""
        size, lev = sizer.calculate(
            entry=50000, sl=49500, capital=1000, risk_pct=0.02
        )
        # distance = 500, risk = 20, size = 0.04
        # leverage = (0.04 * 50000) / 1000 = 2.0
        assert pytest.approx(size, rel=1e-6) == 0.04
        assert pytest.approx(lev, rel=1e-6) == 2.0

    def test_eth_pair(self, sizer):
        """Works with ETH-scale prices too."""
        size, lev = sizer.calculate(
            entry=3000, sl=2950, capital=100, risk_pct=0.02
        )
        # risk = 2, distance = 50, size = 0.04 ETH
        # leverage = (0.04 * 3000) / 100 = 1.2
        assert pytest.approx(size, rel=1e-6) == 0.04
        assert pytest.approx(lev, rel=1e-6) == 1.2


# ============================================================
# Leverage cap
# ============================================================

class TestLeverageCap:
    """Test that leverage is capped at MAX_LEVERAGE."""

    def test_leverage_capped_at_max(self, sizer):
        """Very tight SL would require >5x, should be capped."""
        size, lev = sizer.calculate(
            entry=50000, sl=49900, capital=1000, risk_pct=0.02
        )
        # Uncapped: distance=100, risk=20, size=0.2, lev=(0.2*50000)/1000=10x
        # Capped: lev=5, notional=5000, size=5000/50000=0.1
        assert lev == float(settings.MAX_LEVERAGE)
        assert pytest.approx(size, rel=1e-6) == 0.1

    def test_exactly_at_max_leverage(self, sizer):
        """Exactly at max leverage should not be capped."""
        # distance = 200, risk = 20, size = 0.1
        # lev = (0.1 * 50000) / 1000 = 5.0
        size, lev = sizer.calculate(
            entry=50000, sl=49800, capital=1000, risk_pct=0.02
        )
        assert pytest.approx(lev, rel=1e-6) == 5.0
        assert pytest.approx(size, rel=1e-6) == 0.1

    def test_below_max_leverage_not_capped(self, sizer):
        """Below max leverage should pass through unchanged."""
        size, lev = sizer.calculate(
            entry=50000, sl=49000, capital=1000, risk_pct=0.02
        )
        assert lev < settings.MAX_LEVERAGE
        assert pytest.approx(lev, rel=1e-6) == 1.0


# ============================================================
# Edge cases
# ============================================================

class TestEdgeCases:
    """Test validation and edge cases."""

    def test_entry_equals_sl_raises(self, sizer):
        with pytest.raises(ValueError, match="same price"):
            sizer.calculate(entry=50000, sl=50000, capital=1000, risk_pct=0.02)

    def test_zero_capital_raises(self, sizer):
        with pytest.raises(ValueError, match="Capital must be positive"):
            sizer.calculate(entry=50000, sl=49000, capital=0, risk_pct=0.02)

    def test_negative_capital_raises(self, sizer):
        with pytest.raises(ValueError, match="Capital must be positive"):
            sizer.calculate(entry=50000, sl=49000, capital=-100, risk_pct=0.02)

    def test_zero_risk_pct_raises(self, sizer):
        with pytest.raises(ValueError, match="Risk percent must be positive"):
            sizer.calculate(entry=50000, sl=49000, capital=1000, risk_pct=0)

    def test_negative_risk_pct_raises(self, sizer):
        with pytest.raises(ValueError, match="Risk percent must be positive"):
            sizer.calculate(entry=50000, sl=49000, capital=1000, risk_pct=-0.01)

    def test_small_capital(self, sizer):
        """$50 demo capital should still work."""
        size, lev = sizer.calculate(
            entry=50000, sl=49000, capital=50, risk_pct=0.02
        )
        # risk = 1, distance = 1000, size = 0.001
        assert pytest.approx(size, rel=1e-6) == 0.001
        assert pytest.approx(lev, rel=1e-6) == 1.0


# ============================================================
# Force max leverage mode
# ============================================================

class TestForceMaxLeverage:
    """FORCE_MAX_LEVERAGE ignores risk-based sizing and uses full capital."""

    def test_force_max_leverage_btc(self, sizer):
        """$100 capital at 5x → $500 notional for BTC."""
        with patch.object(settings, "FORCE_MAX_LEVERAGE", True):
            size, lev = sizer.calculate(
                entry=50000, sl=49000, capital=100, risk_pct=0.02
            )
        # notional = 100 * 5 = 500, size = 500 / 50000 = 0.01
        assert lev == float(settings.MAX_LEVERAGE)
        assert pytest.approx(size, rel=1e-6) == 0.01

    def test_force_max_leverage_eth(self, sizer):
        """$100 capital at 5x → $500 notional for ETH."""
        with patch.object(settings, "FORCE_MAX_LEVERAGE", True):
            size, lev = sizer.calculate(
                entry=2500, sl=2400, capital=100, risk_pct=0.02
            )
        # notional = 500, size = 500 / 2500 = 0.2
        assert lev == float(settings.MAX_LEVERAGE)
        assert pytest.approx(size, rel=1e-6) == 0.2

    def test_force_max_leverage_ignores_sl_distance(self, sizer):
        """Position size should be the same regardless of SL distance."""
        with patch.object(settings, "FORCE_MAX_LEVERAGE", True):
            size1, lev1 = sizer.calculate(
                entry=50000, sl=49000, capital=100, risk_pct=0.02
            )
            size2, lev2 = sizer.calculate(
                entry=50000, sl=49900, capital=100, risk_pct=0.02
            )
        assert size1 == size2
        assert lev1 == lev2
