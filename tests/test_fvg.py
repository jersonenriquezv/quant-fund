"""Tests for strategy_service.fvg — Fair Value Gap detection."""

import pytest
from tests.conftest import make_candle
from strategy_service.fvg import FVGDetector, FairValueGap
from config.settings import settings


class TestFVGDetection:
    """Test FVG pattern detection."""

    def test_bullish_fvg_detected(self):
        """candle1.high < candle3.low should produce a bullish FVG."""
        detector = FVGDetector()

        candles = [
            make_candle(high=100.0, low=98.0, close=99.0, timestamp=1000),
            make_candle(high=103.0, low=99.5, close=102.5, timestamp=2000),
            make_candle(high=105.0, low=101.0, close=104.0, timestamp=3000),
        ]
        # candle1.high=100.0 < candle3.low=101.0 → bullish FVG
        # gap: 100.0 to 101.0, size = 1.0/102.5 ≈ 0.97% > 0.1%

        fvgs = detector.update(candles, "BTC/USDT", "15m", 4000)
        bullish = [f for f in fvgs if f.direction == "bullish"]
        assert len(bullish) == 1
        assert bullish[0].low == 100.0
        assert bullish[0].high == 101.0

    def test_bearish_fvg_detected(self):
        """candle1.low > candle3.high should produce a bearish FVG."""
        detector = FVGDetector()

        candles = [
            make_candle(high=105.0, low=103.0, close=104.0, timestamp=1000),
            make_candle(high=102.0, low=99.0, close=99.5, timestamp=2000),
            make_candle(high=101.0, low=98.0, close=99.0, timestamp=3000),
        ]
        # candle1.low=103.0 > candle3.high=101.0 → bearish FVG
        # gap: 101.0 to 103.0, size = 2.0/99.5 ≈ 2.0% > 0.1%

        fvgs = detector.update(candles, "BTC/USDT", "15m", 4000)
        bearish = [f for f in fvgs if f.direction == "bearish"]
        assert len(bearish) == 1
        assert bearish[0].high == 103.0
        assert bearish[0].low == 101.0

    def test_no_fvg_when_wicks_overlap(self):
        """No gap between candle1 and candle3 wicks → no FVG."""
        detector = FVGDetector()

        candles = [
            make_candle(high=102.0, low=98.0, close=101.0, timestamp=1000),
            make_candle(high=104.0, low=100.0, close=103.0, timestamp=2000),
            make_candle(high=105.0, low=101.5, close=104.0, timestamp=3000),
        ]
        # candle1.high=102.0 > candle3.low=101.5 → wicks overlap, no bullish FVG

        fvgs = detector.update(candles, "BTC/USDT", "15m", 4000)
        assert len(fvgs) == 0

    def test_fvg_too_small_filtered(self):
        """FVG smaller than FVG_MIN_SIZE_PCT should be filtered."""
        detector = FVGDetector()

        # Create tiny gap: 100.0 to 100.005 = 0.005% < 0.1%
        candles = [
            make_candle(high=100.0, low=99.0, close=99.5, timestamp=1000),
            make_candle(high=100.1, low=99.8, close=100.05, timestamp=2000),
            make_candle(high=100.2, low=100.005, close=100.1, timestamp=3000),
        ]

        fvgs = detector.update(candles, "BTC/USDT", "15m", 4000)
        assert len(fvgs) == 0

    def test_less_than_3_candles(self):
        """Less than 3 candles → no FVGs."""
        detector = FVGDetector()

        candles = [
            make_candle(timestamp=1000),
            make_candle(timestamp=2000),
        ]

        fvgs = detector.update(candles, "BTC/USDT", "15m", 3000)
        assert len(fvgs) == 0


class TestFVGFillTracking:
    """Test FVG fill status tracking."""

    def test_partial_fill_tracked(self):
        """Price entering FVG should update filled_pct."""
        detector = FVGDetector()

        # Create bullish FVG: gap from 100 to 102 (size=2)
        initial_candles = [
            make_candle(high=100.0, low=98.0, close=99.0, timestamp=1000),
            make_candle(high=104.0, low=99.5, close=103.0, timestamp=2000),
            make_candle(high=105.0, low=102.0, close=104.0, timestamp=3000),
        ]

        fvgs = detector.update(initial_candles, "BTC/USDT", "15m", 4000)
        assert len(fvgs) == 1
        assert fvgs[0].filled_pct == 0.0

        # Now price comes down to 101 (fills half the gap)
        fill_candles = initial_candles + [
            make_candle(high=104.0, low=101.0, close=103.0, timestamp=5000),
        ]

        fvgs = detector.update(fill_candles, "BTC/USDT", "15m", 6000)
        assert len(fvgs) == 1
        assert fvgs[0].filled_pct > 0.0

    def test_fully_filled_fvg_pruned(self):
        """FVG fully filled by price action should be removed."""
        detector = FVGDetector()

        # Create bullish FVG: gap from 100 to 102
        initial_candles = [
            make_candle(high=100.0, low=98.0, close=99.0, timestamp=1000),
            make_candle(high=104.0, low=99.5, close=103.0, timestamp=2000),
            make_candle(high=105.0, low=102.0, close=104.0, timestamp=3000),
        ]

        fvgs = detector.update(initial_candles, "BTC/USDT", "15m", 4000)
        assert len(fvgs) == 1

        # Price drops all the way through the gap
        fill_candles = initial_candles + [
            make_candle(high=103.0, low=99.0, close=99.5, timestamp=5000),
        ]

        fvgs = detector.update(fill_candles, "BTC/USDT", "15m", 6000)
        assert len(fvgs) == 0


class TestFVGExpiration:
    """Test FVG expiration logic."""

    def test_expired_fvg_pruned(self):
        """FVG older than FVG_MAX_AGE_HOURS should be removed."""
        detector = FVGDetector()

        candles = [
            make_candle(high=100.0, low=98.0, close=99.0, timestamp=1000),
            make_candle(high=104.0, low=99.5, close=103.0, timestamp=2000),
            make_candle(high=105.0, low=102.0, close=104.0, timestamp=3000),
        ]

        fvgs = detector.update(candles, "BTC/USDT", "15m", 4000)
        assert len(fvgs) == 1

        # Jump forward past expiration (48h + 1ms)
        expired_time = 2000 + (settings.FVG_MAX_AGE_HOURS * 3600 * 1000) + 1
        fvgs = detector.update(candles, "BTC/USDT", "15m", expired_time)
        assert len(fvgs) == 0


class TestFVGGetActive:
    """Test get_active_fvgs()."""

    def test_returns_active_for_correct_key(self):
        detector = FVGDetector()

        candles = [
            make_candle(high=100.0, low=98.0, close=99.0, timestamp=1000),
            make_candle(high=104.0, low=99.5, close=103.0, timestamp=2000),
            make_candle(high=105.0, low=102.0, close=104.0, timestamp=3000),
        ]

        detector.update(candles, "BTC/USDT", "15m", 4000)
        assert len(detector.get_active_fvgs("BTC/USDT", "15m")) == 1
        assert len(detector.get_active_fvgs("ETH/USDT", "15m")) == 0
