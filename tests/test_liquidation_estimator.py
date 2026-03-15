"""Tests for liquidation level estimator."""

import pytest

from data_service.liquidation_estimator import (
    estimate_liquidation_levels,
    LEVERAGE_TIERS,
    LEVERAGE_WEIGHTS,
    MAINTENANCE_MARGIN,
    LiqBin,
)
from tests.conftest import make_candle


class TestEstimateLiquidationLevels:
    """Core estimator logic tests."""

    def test_empty_candles_returns_empty(self):
        result = estimate_liquidation_levels([], 1_000_000, "BTC/USDT")
        assert result == []

    def test_zero_oi_returns_empty(self):
        candles = [make_candle(close=70000, pair="BTC/USDT")]
        result = estimate_liquidation_levels(candles, 0, "BTC/USDT")
        assert result == []

    def test_negative_oi_returns_empty(self):
        candles = [make_candle(close=70000, pair="BTC/USDT")]
        result = estimate_liquidation_levels(candles, -100, "BTC/USDT")
        assert result == []

    def test_single_candle_produces_bins(self):
        candles = [make_candle(close=70000, volume_quote=1000.0, pair="BTC/USDT")]
        oi_usd = 1_000_000
        result = estimate_liquidation_levels(candles, oi_usd, "BTC/USDT")

        assert len(result) > 0
        # Should have both long and short liquidation bins
        has_long = any(b.liq_long_usd > 0 for b in result)
        has_short = any(b.liq_short_usd > 0 for b in result)
        assert has_long
        assert has_short

    def test_total_usd_sums_to_oi(self):
        """Total allocated USD across all bins should equal OI (for both sides)."""
        candles = [
            make_candle(close=70000 + i * 50, volume_quote=1000.0, pair="BTC/USDT")
            for i in range(10)
        ]
        oi_usd = 5_000_000
        result = estimate_liquidation_levels(candles, oi_usd, "BTC/USDT")

        total_long = sum(b.liq_long_usd for b in result)
        total_short = sum(b.liq_short_usd for b in result)

        # Each side should sum approximately to OI (some rounding in binning)
        assert abs(total_long - oi_usd) / oi_usd < 0.01  # Within 1%
        assert abs(total_short - oi_usd) / oi_usd < 0.01

    def test_bins_sorted_by_price(self):
        candles = [
            make_candle(close=70000, volume_quote=500.0, pair="BTC/USDT"),
            make_candle(close=71000, volume_quote=500.0, pair="BTC/USDT"),
        ]
        result = estimate_liquidation_levels(candles, 1_000_000, "BTC/USDT")

        prices = [b.price for b in result]
        assert prices == sorted(prices)

    def test_long_liq_below_price_short_above(self):
        """Long liquidations should cluster below entry, shorts above."""
        price = 70000
        candles = [make_candle(close=price, volume_quote=1000.0, pair="BTC/USDT")]
        result = estimate_liquidation_levels(candles, 1_000_000, "BTC/USDT")

        for b in result:
            if b.liq_long_usd > 0:
                assert b.price < price, f"Long liq at {b.price} should be below {price}"
            if b.liq_short_usd > 0:
                assert b.price > price, f"Short liq at {b.price} should be above {price}"

    def test_volume_weighting(self):
        """Candle with higher volume should contribute more to its liquidation bins."""
        # Two candles at very different prices so their bins don't overlap,
        # one with 100x the volume
        c_low_vol = make_candle(close=50000, volume_quote=100.0, pair="BTC/USDT")
        c_high_vol = make_candle(close=90000, volume_quote=10000.0, pair="BTC/USDT")
        candles = [c_low_vol, c_high_vol]

        result = estimate_liquidation_levels(candles, 1_000_000, "BTC/USDT")

        # Sum total long liq near 50k candle's high-leverage zone vs 90k candle's
        # At 100x: liq_long = close * (1 - 0.01 * 0.996) ≈ close * 0.99
        # 50k -> 49500, 90k -> 89100. No overlap.
        long_near_50k = sum(
            b.liq_long_usd for b in result
            if 49000 < b.price < 50000
        )
        long_near_90k = sum(
            b.liq_long_usd for b in result
            if 88000 < b.price < 90000
        )

        # 90k candle has ~99% of volume, so its nearby bins should be larger
        assert long_near_90k > long_near_50k

    def test_eth_uses_smaller_bins(self):
        """ETH bins should use $2 instead of $50."""
        # Use many candles with small price increments to fill consecutive bins
        candles = [
            make_candle(close=2100 + i * 0.5, volume_quote=100.0, pair="ETH/USDT")
            for i in range(50)
        ]
        result = estimate_liquidation_levels(candles, 500_000, "ETH/USDT")

        if len(result) >= 2:
            # Check that bins are $2 apart (the configured bin size)
            diffs = [result[i + 1].price - result[i].price for i in range(len(result) - 1)]
            min_diff = min(diffs)
            assert min_diff <= 4, f"ETH bin spacing {min_diff} too large (expected ~$2)"

    def test_btc_uses_larger_bins(self):
        """BTC bins should use $50."""
        candles = [make_candle(close=70000, volume_quote=1000.0, pair="BTC/USDT")]
        result = estimate_liquidation_levels(candles, 1_000_000, "BTC/USDT")

        if len(result) >= 2:
            diffs = [result[i + 1].price - result[i].price for i in range(len(result) - 1)]
            min_diff = min(diffs)
            assert min_diff >= 25, f"BTC bin spacing {min_diff} too small (expected ~$50)"

    def test_zero_close_candle_skipped(self):
        """Candles with close <= 0 should be silently skipped."""
        candles = [
            make_candle(close=0, volume_quote=1000.0, pair="BTC/USDT"),
            make_candle(close=70000, volume_quote=1000.0, pair="BTC/USDT"),
        ]
        result = estimate_liquidation_levels(candles, 1_000_000, "BTC/USDT")
        assert len(result) > 0

    def test_zero_volume_uniform_fallback(self):
        """If all candles have zero volume_quote, fall back to uniform weighting."""
        candles = [
            make_candle(close=70000, volume_quote=0.0, pair="BTC/USDT"),
            make_candle(close=71000, volume_quote=0.0, pair="BTC/USDT"),
        ]
        result = estimate_liquidation_levels(candles, 1_000_000, "BTC/USDT")
        # Should still produce bins (uniform fallback, not crash)
        assert len(result) > 0

    def test_leverage_distribution_correctness(self):
        """Verify leverage tiers produce expected liquidation distances."""
        price = 100_000  # Clean number for easy math
        candles = [make_candle(close=price, volume_quote=1000.0, pair="BTC/USDT")]
        result = estimate_liquidation_levels(candles, 1_000_000, "BTC/USDT")

        # 5x leverage: liq_long ~ 100000 * (1 - 0.2 * 0.996) = ~80080
        # 100x leverage: liq_long ~ 100000 * (1 - 0.01 * 0.996) = ~99004
        # So we should see liquidation bins spread from ~80k to ~99k (longs)
        long_prices = [b.price for b in result if b.liq_long_usd > 0]
        assert min(long_prices) < 81000
        assert max(long_prices) > 98000

    def test_many_candles_performance(self):
        """200 candles should complete quickly without issues."""
        candles = [
            make_candle(close=70000 + i * 10, volume_quote=500.0 + i, pair="BTC/USDT")
            for i in range(200)
        ]
        result = estimate_liquidation_levels(candles, 10_000_000, "BTC/USDT")
        assert len(result) > 0
