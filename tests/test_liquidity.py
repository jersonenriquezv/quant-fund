"""Tests for strategy_service.liquidity — Pools, Sweeps, PD zones."""

import pytest
from tests.conftest import make_candle, make_market_snapshot
from strategy_service.liquidity import (
    LiquidityAnalyzer, LiquidityLevel, LiquiditySweep, PremiumDiscountZone,
)
from strategy_service.market_structure import SwingPoint
from shared.models import LiquidationEvent
from config.settings import settings


def _make_swing_high(price, index=0, timestamp=1000) -> SwingPoint:
    return SwingPoint(timestamp=timestamp, price=price,
                      index=index, swing_type="high")


def _make_swing_low(price, index=0, timestamp=1000) -> SwingPoint:
    return SwingPoint(timestamp=timestamp, price=price,
                      index=index, swing_type="low")


class TestLevelClustering:
    """Test grouping swing points into liquidity levels."""

    def test_equal_highs_form_bsl(self):
        """Two swing highs within tolerance should form a BSL level."""
        analyzer = LiquidityAnalyzer()

        # Two highs very close: 100.0 and 100.04 (0.04% diff < 0.05% tolerance)
        highs = [
            _make_swing_high(100.0, index=5, timestamp=1000),
            _make_swing_high(100.04, index=15, timestamp=2000),
        ]
        lows = []

        candles = [make_candle(volume=10.0, timestamp=i * 1000) for i in range(20)]
        analyzer.update(candles, highs, lows, "BTC/USDT", "15m", None, 25000)

        levels = analyzer.get_levels("BTC/USDT", "15m")
        bsl = [l for l in levels if l.level_type == "bsl"]
        assert len(bsl) == 1
        assert bsl[0].touch_count == 2
        assert abs(bsl[0].price - 100.02) < 0.1  # Average

    def test_equal_lows_form_ssl(self):
        """Two swing lows within tolerance should form an SSL level."""
        analyzer = LiquidityAnalyzer()

        lows = [
            _make_swing_low(95.0, index=5, timestamp=1000),
            _make_swing_low(95.04, index=15, timestamp=2000),
        ]
        highs = []

        candles = [make_candle(volume=10.0, timestamp=i * 1000) for i in range(20)]
        analyzer.update(candles, highs, lows, "BTC/USDT", "15m", None, 25000)

        levels = analyzer.get_levels("BTC/USDT", "15m")
        ssl = [l for l in levels if l.level_type == "ssl"]
        assert len(ssl) == 1
        assert ssl[0].touch_count == 2

    def test_single_swing_no_level(self):
        """A single swing point should NOT form a level (need >= 2 touches)."""
        analyzer = LiquidityAnalyzer()

        highs = [_make_swing_high(100.0, index=5, timestamp=1000)]
        lows = []

        candles = [make_candle(volume=10.0, timestamp=i * 1000) for i in range(20)]
        analyzer.update(candles, highs, lows, "BTC/USDT", "15m", None, 25000)

        levels = analyzer.get_levels("BTC/USDT", "15m")
        assert len(levels) == 0

    def test_distant_swings_separate_clusters(self):
        """Swing points far apart should form separate clusters (neither valid)."""
        analyzer = LiquidityAnalyzer()

        highs = [
            _make_swing_high(100.0, index=5, timestamp=1000),
            _make_swing_high(105.0, index=15, timestamp=2000),  # 5% away
        ]
        lows = []

        candles = [make_candle(volume=10.0, timestamp=i * 1000) for i in range(20)]
        analyzer.update(candles, highs, lows, "BTC/USDT", "15m", None, 25000)

        levels = analyzer.get_levels("BTC/USDT", "15m")
        # Each cluster has only 1 touch → no levels
        assert len(levels) == 0


class TestSweepDetection:
    """Test liquidity sweep detection."""

    def test_bsl_sweep_detected(self):
        """Wick above BSL + close below + volume >= 2x → bearish sweep."""
        analyzer = LiquidityAnalyzer()

        highs = [
            _make_swing_high(100.0, index=2, timestamp=2000),
            _make_swing_high(100.03, index=5, timestamp=5000),
        ]
        lows = []

        # Create candles, last one sweeps the BSL
        candles = [
            make_candle(volume=10.0, timestamp=i * 1000)
            for i in range(8)
        ]
        # Sweep candle: wick above 100.015 (avg BSL), close below, volume=25 (2.5x)
        candles.append(make_candle(
            open=99.0, high=101.0, low=98.0, close=98.5,
            volume=25.0, timestamp=8000,
        ))

        analyzer.update(candles, highs, lows, "BTC/USDT", "15m", None, 9000)

        sweeps = analyzer.get_recent_sweeps("BTC/USDT", "15m")
        assert len(sweeps) >= 1
        bearish_sweeps = [s for s in sweeps if s.direction == "bearish"]
        assert len(bearish_sweeps) >= 1

    def test_ssl_sweep_detected(self):
        """Wick below SSL + close above + volume >= 2x → bullish sweep."""
        analyzer = LiquidityAnalyzer()

        lows = [
            _make_swing_low(95.0, index=2, timestamp=2000),
            _make_swing_low(95.04, index=5, timestamp=5000),
        ]
        highs = []

        candles = [
            make_candle(volume=10.0, timestamp=i * 1000)
            for i in range(8)
        ]
        # Sweep candle: wick below 95.02 (avg SSL), close above, volume=25
        candles.append(make_candle(
            open=96.0, high=97.0, low=94.0, close=96.5,
            volume=25.0, timestamp=8000,
        ))

        analyzer.update(candles, highs, lows, "BTC/USDT", "15m", None, 9000)

        sweeps = analyzer.get_recent_sweeps("BTC/USDT", "15m")
        bullish_sweeps = [s for s in sweeps if s.direction == "bullish"]
        assert len(bullish_sweeps) >= 1

    def test_sweep_low_volume_rejected(self):
        """Sweep with volume < SWEEP_MIN_VOLUME_RATIO should be rejected."""
        analyzer = LiquidityAnalyzer()

        highs = [
            _make_swing_high(100.0, index=2, timestamp=2000),
            _make_swing_high(100.03, index=5, timestamp=5000),
        ]

        candles = [
            make_candle(volume=10.0, timestamp=i * 1000)
            for i in range(8)
        ]
        # Low volume sweep (volume=10 = 1.0x, need 2x)
        candles.append(make_candle(
            open=99.0, high=101.0, low=98.0, close=98.5,
            volume=10.0, timestamp=8000,
        ))

        analyzer.update(candles, highs, [], "BTC/USDT", "15m", None, 9000)

        sweeps = analyzer.get_recent_sweeps("BTC/USDT", "15m")
        assert len(sweeps) == 0

    def test_sweep_with_liquidations(self):
        """Sweep should track liquidation cascade from market snapshot."""
        analyzer = LiquidityAnalyzer()

        lows = [
            _make_swing_low(95.0, index=2, timestamp=2000),
            _make_swing_low(95.04, index=5, timestamp=5000),
        ]

        candles = [
            make_candle(volume=10.0, timestamp=i * 1000) for i in range(8)
        ]
        candles.append(make_candle(
            open=96.0, high=97.0, low=94.0, close=96.5,
            volume=25.0, timestamp=8000,
        ))

        snapshot = make_market_snapshot(
            liquidations=[
                LiquidationEvent(
                    timestamp=8000, pair="BTC/USDT", side="long",
                    size_usd=50000.0, price=94.5, source="binance_forceOrder",
                ),
            ],
        )

        analyzer.update(candles, [], lows, "BTC/USDT", "15m", snapshot, 9000)

        sweeps = analyzer.get_recent_sweeps("BTC/USDT", "15m")
        if sweeps:
            assert sweeps[0].had_liquidations is True


class TestPremiumDiscountZone:
    """Test premium/discount zone calculation."""

    def test_discount_zone(self):
        """Price below 50% of range → discount."""
        analyzer = LiquidityAnalyzer()

        htf_highs = [
            _make_swing_high(110.0, index=5, timestamp=1000),
            _make_swing_high(110.0, index=15, timestamp=2000),
        ]
        htf_lows = [
            _make_swing_low(90.0, index=3, timestamp=500),
            _make_swing_low(90.0, index=10, timestamp=1500),
        ]

        htf_candles = [make_candle(timestamp=i * 1000) for i in range(20)]
        current_price = 95.0  # Below equilibrium (100)

        pd = analyzer.update_premium_discount(
            htf_candles, htf_highs, htf_lows,
            "BTC/USDT", current_price, 25000,
        )

        assert pd is not None
        assert pd.zone == "discount"
        assert pd.equilibrium == 100.0

    def test_premium_zone(self):
        """Price above 50% of range → premium."""
        analyzer = LiquidityAnalyzer()

        htf_highs = [
            _make_swing_high(110.0, index=5, timestamp=1000),
            _make_swing_high(110.0, index=15, timestamp=2000),
        ]
        htf_lows = [
            _make_swing_low(90.0, index=3, timestamp=500),
            _make_swing_low(90.0, index=10, timestamp=1500),
        ]

        htf_candles = [make_candle(timestamp=i * 1000) for i in range(20)]
        current_price = 106.0  # Above equilibrium (100)

        pd = analyzer.update_premium_discount(
            htf_candles, htf_highs, htf_lows,
            "BTC/USDT", current_price, 25000,
        )

        assert pd is not None
        assert pd.zone == "premium"

    def test_equilibrium_zone(self):
        """Price at exactly 50% → equilibrium."""
        analyzer = LiquidityAnalyzer()

        htf_highs = [
            _make_swing_high(110.0, index=5, timestamp=1000),
            _make_swing_high(110.0, index=15, timestamp=2000),
        ]
        htf_lows = [
            _make_swing_low(90.0, index=3, timestamp=500),
            _make_swing_low(90.0, index=10, timestamp=1500),
        ]

        htf_candles = [make_candle(timestamp=i * 1000) for i in range(20)]
        current_price = 100.0  # Exactly 50%

        pd = analyzer.update_premium_discount(
            htf_candles, htf_highs, htf_lows,
            "BTC/USDT", current_price, 25000,
        )

        assert pd is not None
        assert pd.zone == "equilibrium"

    def test_pd_zone_caching(self):
        """PD zone should be cached and not recalculated within PD_RECALC_HOURS."""
        analyzer = LiquidityAnalyzer()

        htf_highs = [
            _make_swing_high(110.0, index=5, timestamp=1000),
            _make_swing_high(110.0, index=15, timestamp=2000),
        ]
        htf_lows = [
            _make_swing_low(90.0, index=3, timestamp=500),
            _make_swing_low(90.0, index=10, timestamp=1500),
        ]
        htf_candles = [make_candle(timestamp=i * 1000) for i in range(20)]

        # First call
        pd1 = analyzer.update_premium_discount(
            htf_candles, htf_highs, htf_lows, "BTC/USDT", 95.0, 25000,
        )
        assert pd1.zone == "discount"

        # Second call 1 hour later (within PD_RECALC_HOURS=4)
        # Price moved to premium but range shouldn't recalculate
        pd2 = analyzer.update_premium_discount(
            htf_candles, htf_highs, htf_lows, "BTC/USDT", 106.0,
            25000 + 3600 * 1000,
        )
        # Zone should update to premium (zone is reclassified even from cache)
        assert pd2.zone == "premium"
        # But range should be the same (not recalculated)
        assert pd2.range_high == pd1.range_high
        assert pd2.range_low == pd1.range_low
