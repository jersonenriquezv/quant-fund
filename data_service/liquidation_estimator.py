"""Estimated liquidation level calculator.

Projects where liquidation clusters sit based on OI + recent candle closes
and industry-average leverage distribution. This is an approximation —
real leverage distribution is proprietary exchange data.

Algorithm:
1. Take last N candles (5m) — covers ~17 hours at 200 candles
2. Define leverage tiers with weights (industry-average distribution)
3. For each candle close, for each leverage tier, compute long/short
   liquidation prices and allocate OI proportionally (weighted by volume)
4. Bucket into price bins and sum estimated USD per bin
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from config.settings import settings

if TYPE_CHECKING:
    from shared.models import Candle


# Leverage tiers and their estimated share of total OI.
# Source: industry research — retail-heavy exchanges skew toward 5-25x.
LEVERAGE_TIERS: list[int] = [5, 10, 25, 50, 100]
LEVERAGE_WEIGHTS: list[float] = [0.30, 0.30, 0.20, 0.15, 0.05]

# Maintenance margin rate (OKX perpetuals, tier 1)
MAINTENANCE_MARGIN: float = 0.004


@dataclass
class LiqBin:
    """Single price bin with estimated liquidation USD on each side."""
    price: float
    liq_long_usd: float
    liq_short_usd: float


def _bin_size(pair: str) -> float:
    """Return price bin width for the given pair."""
    if "BTC" in pair:
        return settings.LIQ_BIN_SIZE_BTC
    if "SOL" in pair:
        return settings.LIQ_BIN_SIZE_SOL
    if "DOGE" in pair:
        return settings.LIQ_BIN_SIZE_DOGE
    return settings.LIQ_BIN_SIZE_ETH


def estimate_liquidation_levels(
    candles: list[Candle],
    oi_usd: float,
    pair: str,
) -> list[LiqBin]:
    """Estimate liquidation level distribution from candles and OI.

    Args:
        candles: Recent 5m candles (typically last 200). Must have .close and .volume_quote.
        oi_usd: Current open interest in USD for the pair.
        pair: Trading pair (e.g. "BTC/USDT") — determines bin size.

    Returns:
        List of LiqBin sorted by price ascending. Empty list if no data.
    """
    if not candles or oi_usd <= 0:
        return []

    bin_size = _bin_size(pair)

    # Weight OI distribution by candle volume (higher volume candles
    # represent more position entries at that price level)
    total_volume = sum(c.volume_quote for c in candles)
    if total_volume <= 0:
        # Fallback to uniform weighting if volume data is missing
        total_volume = len(candles)
        volume_weights = [1.0 / total_volume] * len(candles)
    else:
        volume_weights = [c.volume_quote / total_volume for c in candles]

    # Accumulate liquidation USD into bins
    # Key: bin_center_price -> [liq_long_usd, liq_short_usd]
    bins: dict[float, list[float]] = {}

    for candle, vol_weight in zip(candles, volume_weights):
        close = candle.close
        if close <= 0:
            continue

        for leverage, lev_weight in zip(LEVERAGE_TIERS, LEVERAGE_WEIGHTS):
            # Liquidation price formulas:
            # Long: liq = close * (1 - (1/leverage) * (1 - maintenance_margin))
            # Short: liq = close * (1 + (1/leverage) * (1 - maintenance_margin))
            move_pct = (1.0 / leverage) * (1.0 - MAINTENANCE_MARGIN)
            liq_long_price = close * (1.0 - move_pct)
            liq_short_price = close * (1.0 + move_pct)

            # USD allocated to this (candle, leverage) combo
            usd_alloc = oi_usd * vol_weight * lev_weight

            # Bin the long liquidation
            long_bin = round(liq_long_price / bin_size) * bin_size
            if long_bin not in bins:
                bins[long_bin] = [0.0, 0.0]
            bins[long_bin][0] += usd_alloc

            # Bin the short liquidation
            short_bin = round(liq_short_price / bin_size) * bin_size
            if short_bin not in bins:
                bins[short_bin] = [0.0, 0.0]
            bins[short_bin][1] += usd_alloc

    # Convert to sorted list
    result = [
        LiqBin(price=price, liq_long_usd=vals[0], liq_short_usd=vals[1])
        for price, vals in sorted(bins.items())
    ]

    return result
