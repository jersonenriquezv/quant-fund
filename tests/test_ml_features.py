"""
Tests for ML feature extraction — shared/ml_features.py.

Verifies:
- Feature extraction produces expected fields
- Confluence decomposition works correctly
- Missingness flags are set correctly
- Risk context extraction works
- Stale/late entry features are computed
"""

import time
from unittest.mock import MagicMock

import pytest

from shared.models import TradeSetup, MarketSnapshot, FundingRate, OpenInterest, CVDSnapshot
from shared.ml_features import extract_setup_features, extract_risk_context
from tests.conftest import make_market_snapshot


def _make_setup(**kwargs) -> TradeSetup:
    """Create a TradeSetup with sensible defaults."""
    defaults = dict(
        timestamp=int(time.time() * 1000),
        pair="ETH/USDT",
        direction="long",
        setup_type="setup_a",
        entry_price=2000.0,
        sl_price=1980.0,
        tp1_price=2020.0,
        tp2_price=2040.0,
        confluences=[
            "liquidity_sweep_bullish",
            "choch_bullish",
            "order_block_15m",
            "pd_zone_discount",
            "ob_volume_2.3x",
            "cvd_aligned_bullish",
        ],
        htf_bias="bullish",
        ob_timeframe="15m",
    )
    defaults.update(kwargs)
    return TradeSetup(**defaults)


class TestExtractSetupFeatures:
    def test_basic_geometry(self):
        setup = _make_setup()
        features = extract_setup_features(setup, None, 2005.0)

        assert features["pair"] == "ETH/USDT"
        assert features["direction"] == "long"
        assert features["setup_type"] == "setup_a"
        assert features["entry_price"] == 2000.0
        assert features["sl_price"] == 1980.0
        assert features["htf_bias"] == "bullish"
        assert features["ob_timeframe"] == "15m"

    def test_derived_geometry(self):
        setup = _make_setup()
        features = extract_setup_features(setup, None, 2005.0)

        # risk_distance_pct = |2000 - 1980| / 2000 = 0.01
        assert abs(features["risk_distance_pct"] - 0.01) < 1e-6

        # rr_ratio = |2040 - 2000| / |2000 - 1980| = 40/20 = 2.0
        assert abs(features["rr_ratio"] - 2.0) < 1e-6

        # entry_distance_pct = |2005 - 2000| / 2005
        assert abs(features["entry_distance_pct"] - 5.0 / 2005.0) < 1e-6

        # Only structural confluences count: sweep, choch, order_block, pd_zone = 4
        # (ob_volume and cvd_aligned are metrics, not structural)
        assert features["confluence_count"] == 4

    def test_stale_entry_features(self):
        # Setup created 5 minutes ago
        setup = _make_setup(timestamp=int(time.time() * 1000) - 300_000)
        features = extract_setup_features(setup, None, 2005.0)

        # Should be approximately 5 minutes
        assert 4.5 < features["setup_age_minutes"] < 5.5

    def test_confluence_decomposition(self):
        setup = _make_setup()
        features = extract_setup_features(setup, None, 2005.0)

        assert features["has_liquidity_sweep"] is True
        assert features["has_choch"] is True
        assert features["has_bos"] is False
        assert features["has_fvg"] is False
        assert features["has_breaker_block"] is False
        assert features["pd_zone"] == "discount"
        assert features["pd_aligned"] is True  # long + discount = aligned
        assert abs(features["ob_volume_ratio"] - 2.3) < 1e-6
        assert features["cvd_aligned"] is True

    def test_pd_alignment_short_premium(self):
        setup = _make_setup(
            direction="short",
            confluences=["bos_15m", "pd_zone_premium"],
        )
        features = extract_setup_features(setup, None, 2005.0)
        assert features["pd_aligned"] is True
        assert features["has_bos"] is True

    def test_pd_misaligned(self):
        setup = _make_setup(
            direction="long",
            confluences=["choch_bullish", "pd_zone_premium"],
        )
        features = extract_setup_features(setup, None, 2005.0)
        assert features["pd_aligned"] is False

    def test_missingness_no_snapshot(self):
        """When snapshot is None, all has_* flags should be False."""
        setup = _make_setup()
        features = extract_setup_features(setup, None, 2005.0)

        assert features["has_funding"] is False
        assert features["has_oi"] is False
        assert features["has_cvd"] is False
        assert features["has_news"] is False
        assert features["has_whales"] is False
        assert features["funding_rate"] is None
        assert features["oi_usd"] is None
        assert features["cvd_5m"] is None
        assert features["buy_dominance"] is None

    def test_missingness_with_snapshot(self):
        """When snapshot has data, has_* flags should be True."""
        setup = _make_setup()
        snapshot = make_market_snapshot(pair="ETH/USDT")
        features = extract_setup_features(setup, snapshot, 2005.0)

        assert features["has_funding"] is True
        assert features["has_oi"] is True
        assert features["has_cvd"] is True
        assert features["funding_rate"] == 0.0001
        assert features["oi_usd"] == 1_000_000.0
        assert features["cvd_5m"] is not None
        assert features["buy_dominance"] is not None
        # buy_vol=500, sell_vol=400, dominance=500/900
        assert abs(features["buy_dominance"] - 500.0 / 900.0) < 1e-6

    def test_sl_distance_pct(self):
        setup = _make_setup()
        features = extract_setup_features(setup, None, 2005.0)
        # sl_distance_pct = |2005 - 1980| / 2005
        expected = 25.0 / 2005.0
        assert abs(features["sl_distance_pct"] - expected) < 1e-6

    def test_bos_detection_variants(self):
        """BOS can appear as 'bos_5m', 'bos_15m', etc."""
        setup = _make_setup(confluences=["bos_5m", "order_block_5m"])
        features = extract_setup_features(setup, None, 2005.0)
        assert features["has_bos"] is True
        assert features["has_choch"] is False

    def test_setup_id_exists(self):
        """TradeSetup should have a setup_id by default."""
        setup = _make_setup()
        assert hasattr(setup, "setup_id")
        assert len(setup.setup_id) == 16

    def test_sweep_volume_ratio(self):
        setup = _make_setup(confluences=["sweep_volume_3.5x"])
        features = extract_setup_features(setup, None, 2005.0)
        assert abs(features["sweep_volume_ratio"] - 3.5) < 1e-6

    def test_oi_flush_in_confluences(self):
        setup = _make_setup(confluences=["oi_flush", "oi_flush_usd_50000"])
        features = extract_setup_features(setup, None, 2005.0)
        assert features["has_oi_flush"] is True
        assert features["oi_flush_usd"] == 50000.0


class TestExtractRiskContext:
    def test_extracts_risk_state(self):
        mock_risk = MagicMock()
        mock_risk._state.get_capital.return_value = 108.0
        mock_risk._state.get_open_positions_count.return_value = 1
        mock_risk._state.get_daily_dd_pct.return_value = 0.02
        mock_risk._state.get_weekly_dd_pct.return_value = 0.03
        mock_risk._state.get_trades_today_count.return_value = 3

        ctx = extract_risk_context(mock_risk)

        assert ctx["risk_capital"] == 108.0
        assert ctx["risk_open_positions"] == 1
        assert ctx["risk_daily_dd_pct"] == 0.02
        assert ctx["risk_weekly_dd_pct"] == 0.03
        assert ctx["risk_trades_today"] == 3
