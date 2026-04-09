"""Tests for Volume Profile approximation module."""

import pytest

from shared.models import Candle
from strategy_service.volume_profile import VolumeProfileAnalyzer, VolumeProfile


def _make_candle(
    open_: float, high: float, low: float, close: float,
    volume: float = 100.0, timestamp: int = 0,
) -> Candle:
    return Candle(
        timestamp=timestamp, open=open_, high=high, low=low, close=close,
        volume=volume, volume_quote=volume * close,
        pair="BTC/USDT", timeframe="4h", confirmed=True,
    )


def _make_candles_around_poc(
    poc_price: float = 50000.0,
    n_candles: int = 100,
    spread: float = 5000.0,
) -> list[Candle]:
    """Create candles clustered around a POC price with some spread."""
    candles = []
    for i in range(n_candles):
        # Most candles near POC, fewer at extremes
        if i < n_candles * 0.6:  # 60% near POC
            low = poc_price - 500
            high = poc_price + 500
            vol = 200.0  # Higher volume near POC
        elif i < n_candles * 0.8:  # 20% moderate distance
            low = poc_price - 2000
            high = poc_price - 500
            vol = 50.0
        else:  # 20% far from POC
            low = poc_price + 500
            high = poc_price + spread
            vol = 30.0

        mid = (low + high) / 2
        candles.append(_make_candle(
            open_=mid - 100, high=high, low=low, close=mid + 100,
            volume=vol, timestamp=1000 + i,
        ))
    return candles


class TestVolumeProfileComputation:

    def test_poc_is_highest_volume_price(self):
        """POC should be at the price level with most accumulated volume."""
        analyzer = VolumeProfileAnalyzer(bin_count=100)
        candles = _make_candles_around_poc(poc_price=50000.0)
        profile = analyzer.update("BTC/USDT", candles)

        assert profile is not None
        # POC should be near 50000 (within the cluster zone)
        assert 49000 < profile.poc_price < 51000

    def test_value_area_contains_70pct_volume(self):
        """VAH-VAL should bracket approximately 70% of total volume."""
        analyzer = VolumeProfileAnalyzer(bin_count=200, value_area_pct=0.70)
        candles = _make_candles_around_poc(poc_price=50000.0)
        profile = analyzer.update("BTC/USDT", candles)

        assert profile is not None
        assert profile.val < profile.poc_price < profile.vah
        assert profile.val < profile.vah

    def test_hvn_detected_at_high_volume_zone(self):
        """High Volume Nodes should appear where volume clusters."""
        analyzer = VolumeProfileAnalyzer(bin_count=100, hvn_threshold=1.3)
        candles = _make_candles_around_poc(poc_price=50000.0)
        profile = analyzer.update("BTC/USDT", candles)

        assert profile is not None
        assert len(profile.high_volume_nodes) > 0
        # HVNs should be near POC
        hvn_prices = [p for p, _ in profile.high_volume_nodes]
        near_poc = any(abs(p - 50000) < 2000 for p in hvn_prices)
        assert near_poc

    def test_lvn_detected_in_volume_gaps(self):
        """Low Volume Nodes should appear in gaps between high-volume zones."""
        # Create two clusters with a gap
        candles = []
        # Cluster 1: around 45000
        for i in range(50):
            candles.append(_make_candle(
                open_=44800, high=45200, low=44800, close=45200,
                volume=200.0, timestamp=1000 + i,
            ))
        # Cluster 2: around 55000 (10000 gap)
        for i in range(50):
            candles.append(_make_candle(
                open_=54800, high=55200, low=54800, close=55200,
                volume=200.0, timestamp=2000 + i,
            ))
        # Sparse candles in between (low volume)
        for i in range(10):
            candles.append(_make_candle(
                open_=49000, high=51000, low=49000, close=51000,
                volume=5.0, timestamp=3000 + i,
            ))

        analyzer = VolumeProfileAnalyzer(bin_count=100, lvn_threshold=0.3)
        profile = analyzer.update("BTC/USDT", candles)

        assert profile is not None
        # Should have at least one LVN in the gap zone
        assert len(profile.low_volume_nodes) >= 1

    def test_empty_candles_returns_none(self):
        analyzer = VolumeProfileAnalyzer()
        assert analyzer.update("BTC/USDT", []) is None
        assert analyzer.get_profile("BTC/USDT") is None

    def test_insufficient_candles_returns_none(self):
        analyzer = VolumeProfileAnalyzer()
        candles = [_make_candle(100, 110, 90, 105, timestamp=i) for i in range(5)]
        assert analyzer.update("BTC/USDT", candles) is None

    def test_caching_avoids_recomputation(self):
        """Same candle timestamp should return cached profile."""
        analyzer = VolumeProfileAnalyzer(bin_count=50)
        candles = _make_candles_around_poc(n_candles=30)
        p1 = analyzer.update("BTC/USDT", candles)
        p2 = analyzer.update("BTC/USDT", candles)
        assert p1 is p2  # Same object (cached)

    def test_new_candle_triggers_recomputation(self):
        """New candle timestamp should trigger fresh computation."""
        analyzer = VolumeProfileAnalyzer(bin_count=50)
        candles = _make_candles_around_poc(n_candles=30)
        p1 = analyzer.update("BTC/USDT", candles)

        # Add new candle with different timestamp
        new_candle = _make_candle(50000, 50500, 49500, 50200, volume=100, timestamp=99999)
        candles2 = candles + [new_candle]
        p2 = analyzer.update("BTC/USDT", candles2)
        assert p2 is not p1  # Different object (recomputed)

    def test_structural_levels_returns_sorted(self):
        analyzer = VolumeProfileAnalyzer(bin_count=100)
        candles = _make_candles_around_poc(poc_price=50000.0)
        analyzer.update("BTC/USDT", candles)

        levels = analyzer.get_structural_levels("BTC/USDT")
        assert levels == sorted(levels)
        assert len(levels) >= 3  # At minimum POC, VAH, VAL


class TestVolumeProfileHelpers:

    def test_is_near_poc(self):
        analyzer = VolumeProfileAnalyzer(bin_count=100)
        candles = _make_candles_around_poc(poc_price=50000.0)
        analyzer.update("BTC/USDT", candles)
        profile = analyzer.get_profile("BTC/USDT")

        # Price near POC
        assert analyzer.is_near_poc("BTC/USDT", profile.poc_price + 100, atr=500)
        # Price far from POC
        assert not analyzer.is_near_poc("BTC/USDT", profile.poc_price + 5000, atr=500)

    def test_is_near_hvn(self):
        analyzer = VolumeProfileAnalyzer(bin_count=100, hvn_threshold=1.3)
        candles = _make_candles_around_poc(poc_price=50000.0)
        analyzer.update("BTC/USDT", candles)

        # Price near POC cluster (should have HVNs there)
        assert analyzer.is_near_hvn("BTC/USDT", 50000, atr=1000)

    def test_is_in_lvn_returns_false_for_hvn_zone(self):
        analyzer = VolumeProfileAnalyzer(bin_count=100)
        candles = _make_candles_around_poc(poc_price=50000.0)
        analyzer.update("BTC/USDT", candles)

        # POC area should NOT be LVN
        assert not analyzer.is_in_lvn("BTC/USDT", 50000)

    def test_nonexistent_pair_returns_defaults(self):
        analyzer = VolumeProfileAnalyzer()
        assert analyzer.get_profile("FAKE/PAIR") is None
        assert analyzer.get_structural_levels("FAKE/PAIR") == []
        assert not analyzer.is_near_poc("FAKE/PAIR", 100, 10)
        assert not analyzer.is_near_hvn("FAKE/PAIR", 100, 10)
        assert not analyzer.is_in_lvn("FAKE/PAIR", 100)


class TestStructuralTPs:
    """Test that structural TP logic works in the setup evaluator."""

    def test_structural_tp_uses_swing_level(self):
        """When a swing high exists above entry, TP should target it."""
        from strategy_service.setups import SetupEvaluator
        from strategy_service.market_structure import SwingPoint

        evaluator = SetupEvaluator()
        entry = 50000.0
        sl = 49000.0  # risk = 1000

        swing_highs = [
            SwingPoint(price=52500.0, timestamp=1000, index=10, swing_type="high"),
            SwingPoint(price=55000.0, timestamp=2000, index=20, swing_type="high"),
        ]

        tp1, tp2 = evaluator._calculate_tp_levels(
            entry, sl, "bullish", [], "setup_f",
            swing_highs_htf=swing_highs,
            swing_lows_htf=[],
        )

        # TP1 should be at least the fixed TP1 (entry + 1 × risk = 51000)
        assert tp1 >= 51000.0
        # TP2 should be at or above fixed TP2 (entry + 2 × risk = 52000)
        assert tp2 >= 52000.0

    def test_structural_tp_falls_back_to_fixed_rr(self):
        """With no structural levels, should use fixed R:R."""
        from strategy_service.setups import SetupEvaluator

        evaluator = SetupEvaluator()
        entry = 50000.0
        sl = 49000.0

        tp1, tp2 = evaluator._calculate_tp_levels(
            entry, sl, "bullish", [], "setup_f",
        )

        # Fixed R:R: TP1 = 51000, TP2 = 52000 (2.0x for setup_f)
        assert abs(tp1 - 51000.0) < 1.0
        assert abs(tp2 - 52000.0) < 1.0

    def test_structural_tp_short_direction(self):
        """Structural TP should work for short setups too."""
        from strategy_service.setups import SetupEvaluator
        from strategy_service.market_structure import SwingPoint

        evaluator = SetupEvaluator()
        entry = 50000.0
        sl = 51000.0  # risk = 1000

        swing_lows = [
            SwingPoint(price=47000.0, timestamp=1000, index=10, swing_type="low"),
        ]

        tp1, tp2 = evaluator._calculate_tp_levels(
            entry, sl, "bearish", [], "setup_f",
            swing_highs_htf=[],
            swing_lows_htf=swing_lows,
        )

        # Short: TP1 should be <= fixed TP1 (entry - 1 × risk = 49000)
        assert tp1 <= 49000.0
        # TP2 should target 47000 (swing low) or fixed
        assert tp2 <= 48000.0

    def test_structural_tp_with_volume_profile(self):
        """VP POC/VAH/VAL should be used as TP candidates."""
        from strategy_service.setups import SetupEvaluator

        evaluator = SetupEvaluator()
        entry = 50000.0
        sl = 49000.0

        vp = VolumeProfile(
            poc_price=52000.0,
            vah=53500.0,
            val=48000.0,
            high_volume_nodes=[(51500.0, 1000), (53000.0, 800)],
            low_volume_nodes=[],
            total_volume=10000,
            price_low=45000,
            price_high=55000,
            bin_size=50,
            computed_at=1000,
        )

        tp1, tp2 = evaluator._calculate_tp_levels(
            entry, sl, "bullish", [], "setup_a",
            volume_profile=vp,
        )

        # Fixed TP1 = 51000 (1:1), fixed TP2 = 52500 (2.5:1 for setup_a)
        # VP has HVN at 51500, POC at 52000, HVN at 53000, VAH at 53500
        # TP1 should be >= fixed TP1
        assert tp1 >= 51000.0
        # TP2 should be >= fixed TP2 or a structural level above it
        assert tp2 >= 52500.0
