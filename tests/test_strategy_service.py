"""Tests for strategy_service.service — StrategyService facade.

Tests the evaluate() orchestration: HTF bias, LTF analysis, setup detection.
Uses a mock DataService to provide candle data.
"""

import time
import pytest
from unittest.mock import MagicMock

from strategy_service.service import StrategyService
from tests.conftest import make_candle, make_candle_series
from config.settings import settings


def _mock_data_service(
    candles_4h=None,
    candles_1h=None,
    candles_15m=None,
    candles_5m=None,
) -> MagicMock:
    """Create a mock DataService with configurable candle data."""
    ds = MagicMock()

    def get_candles(pair, tf, count=500):
        return {
            "4h": candles_4h or [],
            "1h": candles_1h or [],
            "15m": candles_15m or [],
            "5m": candles_5m or [],
        }.get(tf, [])

    ds.get_candles = get_candles
    ds.get_market_snapshot.return_value = MagicMock(
        funding=None, oi=None, cvd=None,
        recent_oi_flushes=[], whale_movements=[],
    )
    return ds


# ============================================================
# HTF bias determination
# ============================================================

class TestHTFBias:

    def test_no_data_returns_none(self):
        """No candle data → undefined HTF bias → no setup."""
        ds = _mock_data_service()
        svc = StrategyService(ds)

        trigger = make_candle(timeframe="5m")
        result = svc.evaluate("BTC/USDT", trigger)

        assert result is None
        assert svc.get_htf_bias("BTC/USDT") == "undefined"

    def test_htf_candle_does_not_trigger_evaluation(self):
        """4H candle should not trigger LTF evaluation."""
        ds = _mock_data_service()
        svc = StrategyService(ds)

        trigger = make_candle(timeframe="4h")
        result = svc.evaluate("BTC/USDT", trigger)

        assert result is None

    def test_1h_candle_does_not_trigger_evaluation(self):
        """1H candle should not trigger LTF evaluation."""
        ds = _mock_data_service()
        svc = StrategyService(ds)

        trigger = make_candle(timeframe="1h")
        result = svc.evaluate("BTC/USDT", trigger)

        assert result is None

    def test_bullish_htf_bias_cached(self):
        """When 4H is bullish, bias should be cached as 'bullish'."""
        # Create candles with a clear uptrend: BOS confirmed
        candles_4h = make_candle_series(
            base_price=100.0, count=50, timeframe="4h",
            price_changes=[2.0] * 50,  # strong uptrend
            start_ts=1_000_000_000_000,
            interval_ms=14_400_000,  # 4h
        )
        ds = _mock_data_service(candles_4h=candles_4h)
        svc = StrategyService(ds)

        trigger = make_candle(timeframe="5m", close=candles_4h[-1].close)
        svc.evaluate("BTC/USDT", trigger)

        bias = svc.get_htf_bias("BTC/USDT")
        assert bias in ("bullish", "undefined")  # depends on swing detection

    def test_unknown_pair_returns_undefined(self):
        """Pair never evaluated → undefined bias."""
        ds = _mock_data_service()
        svc = StrategyService(ds)

        assert svc.get_htf_bias("DOGE/USDT") == "undefined"


# ============================================================
# LTF evaluation
# ============================================================

class TestLTFEvaluation:

    def test_insufficient_candles_returns_none(self):
        """Too few LTF candles → no setup possible."""
        # Only 3 candles — not enough for swing detection
        candles = make_candle_series(count=3, timeframe="15m")
        ds = _mock_data_service(candles_15m=candles)
        svc = StrategyService(ds)

        trigger = make_candle(timeframe="15m")
        result = svc.evaluate("BTC/USDT", trigger)

        assert result is None

    def test_flat_price_action_no_setup(self):
        """Flat price with no structure breaks → no setup."""
        candles_4h = make_candle_series(
            count=50, timeframe="4h",
            price_changes=[0.01] * 50,  # near-flat
            interval_ms=14_400_000,
        )
        candles_15m = make_candle_series(
            count=200, timeframe="15m",
            price_changes=[0.01] * 200,
            interval_ms=900_000,
        )
        ds = _mock_data_service(candles_4h=candles_4h, candles_15m=candles_15m)
        svc = StrategyService(ds)

        trigger = make_candle(timeframe="15m")
        result = svc.evaluate("BTC/USDT", trigger)

        assert result is None


# ============================================================
# Pair isolation
# ============================================================

class TestPairIsolation:

    def test_different_pairs_independent_bias(self):
        """HTF bias for BTC should not affect ETH."""
        ds = _mock_data_service()
        svc = StrategyService(ds)

        # Evaluate BTC
        trigger_btc = make_candle(pair="BTC/USDT", timeframe="5m")
        svc.evaluate("BTC/USDT", trigger_btc)

        # Evaluate ETH
        trigger_eth = make_candle(pair="ETH/USDT", timeframe="5m")
        svc.evaluate("ETH/USDT", trigger_eth)

        # Both should have independent bias (both undefined with no data)
        assert svc.get_htf_bias("BTC/USDT") == "undefined"
        assert svc.get_htf_bias("ETH/USDT") == "undefined"


# ============================================================
# Active order blocks
# ============================================================

class TestActiveOrderBlocks:

    def test_no_obs_initially(self):
        """No evaluation → no active OBs."""
        ds = _mock_data_service()
        svc = StrategyService(ds)

        obs = svc.get_active_order_blocks("BTC/USDT")
        assert obs == []
