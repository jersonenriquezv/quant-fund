"""
Liquidity Analysis — Pools, Sweeps, Premium/Discount Zones.

Detects:
- Liquidity levels: clusters of swing highs (BSL) or swing lows (SSL)
- Liquidity sweeps: wick breaks level but close stays inside
- Premium/Discount zones: above/below 50% of HTF range

All thresholds from config.settings.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from config.settings import settings
from shared.models import Candle, MarketSnapshot
from strategy_service.market_structure import SwingPoint


@dataclass
class LiquidityLevel:
    """A cluster of swing points forming a liquidity pool."""
    price: float                # Average price of the cluster
    level_type: str             # "bsl" (buy-side) or "ssl" (sell-side)
    touch_count: int            # Number of swing points in cluster
    timestamps: list[int]       # Timestamps of contributing swings
    swept: bool = False         # True if this level has been swept


@dataclass
class LiquiditySweep:
    """A detected liquidity sweep event."""
    timestamp: int
    pair: str
    timeframe: str
    direction: str              # "bullish" (swept SSL, expect up) or "bearish" (swept BSL, expect down)
    swept_level: float          # The liquidity level that was swept
    wick_price: float           # How far the wick went
    close_price: float          # Where the candle closed
    volume_ratio: float         # Candle volume / average volume
    had_liquidations: bool      # True if liquidation cascade detected


@dataclass
class PremiumDiscountZone:
    """Premium/Discount zone calculated from HTF swing range."""
    pair: str
    range_high: float           # HTF swing high
    range_low: float            # HTF swing low
    equilibrium: float          # 50% level
    last_updated_ms: int
    zone: str = "undefined"     # "premium", "discount", or "equilibrium"


class LiquidityAnalyzer:
    """Detects liquidity pools, sweeps, and premium/discount zones.

    State persists between calls for liquidity levels and PD zones.
    """

    def __init__(self):
        # Key: "pair:timeframe"
        self._levels: dict[str, list[LiquidityLevel]] = {}
        self._sweeps: dict[str, list[LiquiditySweep]] = {}
        # Key: pair
        self._pd_zones: dict[str, PremiumDiscountZone] = {}

    def update(
        self,
        candles: list[Candle],
        swing_highs: list[SwingPoint],
        swing_lows: list[SwingPoint],
        pair: str,
        timeframe: str,
        market_snapshot: Optional[MarketSnapshot],
        current_time_ms: int,
    ) -> None:
        """Update liquidity levels and detect sweeps.

        Args:
            candles: OHLCV candles, oldest first.
            swing_highs: Detected swing highs from market structure.
            swing_lows: Detected swing lows from market structure.
            pair: e.g. "BTC/USDT"
            timeframe: e.g. "15m"
            market_snapshot: For liquidation data. Can be None.
            current_time_ms: Current time in milliseconds.
        """
        key = f"{pair}:{timeframe}"

        # Cluster swing points into liquidity levels
        bsl_levels = self._cluster_levels(swing_highs, "bsl")
        ssl_levels = self._cluster_levels(swing_lows, "ssl")
        new_levels = bsl_levels + ssl_levels

        # Merge with existing levels to preserve swept status
        self._levels[key] = self._merge_levels(
            self._levels.get(key, []), new_levels
        )

        # Detect sweeps
        avg_volume = self._compute_avg_volume(candles)
        new_sweeps = self._detect_sweeps(
            candles, self._levels[key], avg_volume,
            pair, timeframe, market_snapshot
        )

        if key not in self._sweeps:
            self._sweeps[key] = []

        # Deduplicate sweeps by timestamp
        existing_ts = {s.timestamp for s in self._sweeps[key]}
        for sweep in new_sweeps:
            if sweep.timestamp not in existing_ts:
                self._sweeps[key].append(sweep)

        # Prune old sweeps (keep last 24h)
        max_age_ms = 24 * 3600 * 1000
        self._sweeps[key] = [
            s for s in self._sweeps[key]
            if (current_time_ms - s.timestamp) <= max_age_ms
        ]

    def update_premium_discount(
        self,
        htf_candles: list[Candle],
        htf_swing_highs: list[SwingPoint],
        htf_swing_lows: list[SwingPoint],
        pair: str,
        current_price: float,
        current_time_ms: int,
    ) -> Optional[PremiumDiscountZone]:
        """Recalculate premium/discount zone from HTF (4H) swings.

        Only recalculates if PD_RECALC_HOURS have passed since last update.

        Args:
            htf_candles: 4H candles for range calculation.
            htf_swing_highs: 4H swing highs.
            htf_swing_lows: 4H swing lows.
            pair: e.g. "BTC/USDT"
            current_price: Current market price.
            current_time_ms: Current time in milliseconds.

        Returns:
            Updated PremiumDiscountZone or cached one.
        """
        existing = self._pd_zones.get(pair)
        recalc_ms = settings.PD_RECALC_HOURS * 3600 * 1000

        # Check if recalculation is needed
        if existing and (current_time_ms - existing.last_updated_ms) < recalc_ms:
            # Just update the zone classification based on current price
            existing.zone = self._classify_zone(
                current_price, existing.range_high, existing.range_low
            )
            return existing

        # Find the most recent swing high and swing low
        if not htf_swing_highs or not htf_swing_lows:
            # Fallback: use recent candle range
            if len(htf_candles) < 2:
                return existing

            range_high = max(c.high for c in htf_candles[-20:]) if len(htf_candles) >= 20 else max(c.high for c in htf_candles)
            range_low = min(c.low for c in htf_candles[-20:]) if len(htf_candles) >= 20 else min(c.low for c in htf_candles)
        else:
            range_high = max(sh.price for sh in htf_swing_highs[-5:])
            range_low = min(sl.price for sl in htf_swing_lows[-5:])

        if range_high <= range_low:
            return existing

        equilibrium = (range_high + range_low) / 2
        zone = self._classify_zone(current_price, range_high, range_low)

        pd_zone = PremiumDiscountZone(
            pair=pair,
            range_high=range_high,
            range_low=range_low,
            equilibrium=equilibrium,
            last_updated_ms=current_time_ms,
            zone=zone,
        )
        self._pd_zones[pair] = pd_zone
        return pd_zone

    def get_pd_zone(self, pair: str) -> Optional[PremiumDiscountZone]:
        """Get cached premium/discount zone for a pair."""
        return self._pd_zones.get(pair)

    def get_recent_sweeps(self, pair: str,
                          timeframe: str) -> list[LiquiditySweep]:
        """Get recent liquidity sweeps for a pair+timeframe."""
        return list(self._sweeps.get(f"{pair}:{timeframe}", []))

    def get_levels(self, pair: str,
                   timeframe: str) -> list[LiquidityLevel]:
        """Get current liquidity levels for a pair+timeframe."""
        return list(self._levels.get(f"{pair}:{timeframe}", []))

    def _cluster_levels(
        self, swings: list[SwingPoint], level_type: str
    ) -> list[LiquidityLevel]:
        """Group swing points within EQUAL_LEVEL_TOLERANCE_PCT into clusters.

        A valid liquidity level needs >= 2 touches (swing points).
        """
        if not swings:
            return []

        tolerance_pct = settings.EQUAL_LEVEL_TOLERANCE_PCT
        sorted_swings = sorted(swings, key=lambda s: s.price)

        clusters: list[list[SwingPoint]] = []
        current_cluster: list[SwingPoint] = [sorted_swings[0]]

        for i in range(1, len(sorted_swings)):
            prev_price = current_cluster[-1].price
            curr_price = sorted_swings[i].price

            # Check if within tolerance
            if prev_price > 0 and abs(curr_price - prev_price) / prev_price <= tolerance_pct:
                current_cluster.append(sorted_swings[i])
            else:
                clusters.append(current_cluster)
                current_cluster = [sorted_swings[i]]

        clusters.append(current_cluster)

        # Only keep clusters with >= 2 touches
        levels: list[LiquidityLevel] = []
        for cluster in clusters:
            if len(cluster) >= 2:
                avg_price = sum(s.price for s in cluster) / len(cluster)
                levels.append(LiquidityLevel(
                    price=avg_price,
                    level_type=level_type,
                    touch_count=len(cluster),
                    timestamps=[s.timestamp for s in cluster],
                ))

        return levels

    def _merge_levels(
        self,
        old_levels: list[LiquidityLevel],
        new_levels: list[LiquidityLevel],
    ) -> list[LiquidityLevel]:
        """Merge new levels with existing ones, preserving swept status.

        If a new level matches an old one by price proximity, carry over
        the swept flag so previously-swept levels aren't re-detected.
        """
        tolerance = settings.EQUAL_LEVEL_TOLERANCE_PCT

        for new_level in new_levels:
            for old_level in old_levels:
                if old_level.level_type != new_level.level_type:
                    continue
                if old_level.price <= 0:
                    continue
                # Match by price proximity
                diff_pct = abs(new_level.price - old_level.price) / old_level.price
                if diff_pct <= tolerance:
                    # Carry over swept status
                    if old_level.swept:
                        new_level.swept = True
                    break

        return new_levels

    def _detect_sweeps(
        self,
        candles: list[Candle],
        levels: list[LiquidityLevel],
        avg_volume: float,
        pair: str,
        timeframe: str,
        market_snapshot: Optional[MarketSnapshot],
    ) -> list[LiquiditySweep]:
        """Detect liquidity sweeps.

        Sweep = wick breaks level but close stays inside range.
        Volume must be >= SWEEP_MIN_VOLUME_RATIO * average.
        """
        sweeps: list[LiquiditySweep] = []
        min_vol_ratio = settings.SWEEP_MIN_VOLUME_RATIO

        # Check if there were recent liquidations
        had_liquidations = False
        if market_snapshot and market_snapshot.recent_liquidations:
            had_liquidations = len(market_snapshot.recent_liquidations) > 0

        for level in levels:
            if level.swept:
                continue

            for candle in candles:
                vol_ratio = (
                    candle.volume / avg_volume if avg_volume > 0 else 0.0
                )

                if level.level_type == "bsl":
                    # BSL sweep: wick goes above level, close stays below
                    if candle.high > level.price and candle.close < level.price:
                        if vol_ratio >= min_vol_ratio:
                            level.swept = True
                            sweeps.append(LiquiditySweep(
                                timestamp=candle.timestamp,
                                pair=pair,
                                timeframe=timeframe,
                                direction="bearish",  # Swept BSL = expect reversal down
                                swept_level=level.price,
                                wick_price=candle.high,
                                close_price=candle.close,
                                volume_ratio=vol_ratio,
                                had_liquidations=had_liquidations,
                            ))
                            break

                elif level.level_type == "ssl":
                    # SSL sweep: wick goes below level, close stays above
                    if candle.low < level.price and candle.close > level.price:
                        if vol_ratio >= min_vol_ratio:
                            level.swept = True
                            sweeps.append(LiquiditySweep(
                                timestamp=candle.timestamp,
                                pair=pair,
                                timeframe=timeframe,
                                direction="bullish",  # Swept SSL = expect reversal up
                                swept_level=level.price,
                                wick_price=candle.low,
                                close_price=candle.close,
                                volume_ratio=vol_ratio,
                                had_liquidations=had_liquidations,
                            ))
                            break

        return sweeps

    def _compute_avg_volume(self, candles: list[Candle]) -> float:
        """Compute average volume over VOLUME_AVG_PERIODS candles."""
        periods = settings.VOLUME_AVG_PERIODS
        relevant = candles[-periods:] if len(candles) >= periods else candles

        if not relevant:
            return 0.0

        return sum(c.volume for c in relevant) / len(relevant)

    def _classify_zone(self, price: float, range_high: float,
                       range_low: float) -> str:
        """Classify current price as premium, discount, or equilibrium.

        Uses a tolerance band around 50% to define equilibrium zone.
        E.g. with PD_EQUILIBRIUM_BAND=0.02, positions 0.48-0.52 = equilibrium.
        """
        if range_high <= range_low:
            return "undefined"

        total_range = range_high - range_low
        position = (price - range_low) / total_range

        band = settings.PD_EQUILIBRIUM_BAND
        if position > (0.5 + band):
            return "premium"
        elif position < (0.5 - band):
            return "discount"
        else:
            return "equilibrium"
