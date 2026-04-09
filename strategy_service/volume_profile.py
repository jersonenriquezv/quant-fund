"""
Volume Profile approximation from OHLCV candles.

Distributes each candle's volume uniformly across [low, high] price range,
accumulates over a lookback window, and identifies key levels:
- POC (Point of Control): price with most accumulated volume
- VAH/VAL (Value Area High/Low): price range containing 70% of volume
- HVN (High Volume Nodes): price levels with volume > 1.5x median
- LVN (Low Volume Nodes): contiguous gaps with volume < 0.5x median

This is an approximation — real VP requires tick data. Candle-based VP is
what TradingView uses for "Volume Profile Fixed Range" and is sufficient
for identifying major institutional zones.

Usage:
    analyzer = VolumeProfileAnalyzer()
    profile = analyzer.update("BTC/USDT", candles_4h)
    if profile:
        print(f"POC: {profile.poc_price}, VAH: {profile.vah}, VAL: {profile.val}")
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np

from shared.logger import setup_logger
from shared.models import Candle

logger = setup_logger("volume_profile")


@dataclass(frozen=True)
class VolumeProfile:
    """Computed volume profile for a pair."""
    poc_price: float           # Point of Control (price bin with max volume)
    vah: float                 # Value Area High (upper bound of 70% volume)
    val: float                 # Value Area Low (lower bound of 70% volume)
    high_volume_nodes: list    # [(price, volume), ...] for HVNs
    low_volume_nodes: list     # [(price_low, price_high), ...] for LVN gaps
    total_volume: float
    price_low: float           # Lowest price in profile range
    price_high: float          # Highest price in profile range
    bin_size: float            # Price width per bin
    computed_at: int           # Timestamp ms of newest candle used


class VolumeProfileAnalyzer:
    """Computes and caches Volume Profiles per pair from 4H candles."""

    def __init__(
        self,
        bin_count: int = 200,
        value_area_pct: float = 0.70,
        hvn_threshold: float = 1.5,
        lvn_threshold: float = 0.5,
    ):
        self._bin_count = bin_count
        self._value_area_pct = value_area_pct
        self._hvn_threshold = hvn_threshold
        self._lvn_threshold = lvn_threshold

        # Cache: pair -> (VolumeProfile, last_candle_timestamp)
        self._profiles: dict[str, tuple[VolumeProfile, int]] = {}

    def update(
        self,
        pair: str,
        candles: list[Candle],
    ) -> Optional[VolumeProfile]:
        """Recompute VP if new candle data available. Returns cached if unchanged.

        Args:
            pair: e.g. "BTC/USDT"
            candles: 4H candles (oldest first), ideally 500+ for 83-day lookback.

        Returns:
            VolumeProfile or None if insufficient data.
        """
        if not candles or len(candles) < 20:
            return self._profiles.get(pair, (None, 0))[0]

        latest_ts = candles[-1].timestamp
        cached = self._profiles.get(pair)
        if cached and cached[1] == latest_ts:
            return cached[0]  # No new candle, return cached

        profile = self._compute(candles)
        if profile:
            self._profiles[pair] = (profile, latest_ts)
            logger.info(
                f"VP updated [{pair}]: POC={profile.poc_price:.2f} "
                f"VAH={profile.vah:.2f} VAL={profile.val:.2f} "
                f"HVN={len(profile.high_volume_nodes)} LVN={len(profile.low_volume_nodes)}"
            )
        return profile

    def get_profile(self, pair: str) -> Optional[VolumeProfile]:
        """Get cached profile for a pair."""
        cached = self._profiles.get(pair)
        return cached[0] if cached else None

    def get_structural_levels(self, pair: str) -> list[float]:
        """Return POC, VAH, VAL as sorted price levels for TP targeting."""
        profile = self.get_profile(pair)
        if not profile:
            return []
        levels = [profile.poc_price, profile.vah, profile.val]
        # Also add HVN prices
        for price, _ in profile.high_volume_nodes:
            levels.append(price)
        return sorted(set(levels))

    def is_near_hvn(
        self, pair: str, price: float, atr: float,
    ) -> bool:
        """Check if price is within 1x ATR of any High Volume Node."""
        profile = self.get_profile(pair)
        if not profile or atr <= 0:
            return False
        for hvn_price, _ in profile.high_volume_nodes:
            if abs(price - hvn_price) <= atr:
                return True
        return False

    def is_near_poc(
        self, pair: str, price: float, atr: float,
    ) -> bool:
        """Check if price is within 1x ATR of the POC."""
        profile = self.get_profile(pair)
        if not profile or atr <= 0:
            return False
        return abs(price - profile.poc_price) <= atr

    def is_in_lvn(self, pair: str, price: float) -> bool:
        """Check if price falls in a Low Volume Node gap."""
        profile = self.get_profile(pair)
        if not profile:
            return False
        for lvn_low, lvn_high in profile.low_volume_nodes:
            if lvn_low <= price <= lvn_high:
                return True
        return False

    def _compute(self, candles: list[Candle]) -> Optional[VolumeProfile]:
        """Build volume profile from candles using uniform distribution."""
        # Determine price range
        all_lows = [c.low for c in candles if c.low > 0]
        all_highs = [c.high for c in candles if c.high > 0]
        if not all_lows or not all_highs:
            return None

        price_low = min(all_lows)
        price_high = max(all_highs)
        if price_high <= price_low:
            return None

        bin_count = self._bin_count
        bin_size = (price_high - price_low) / bin_count
        if bin_size <= 0:
            return None

        # Accumulate volume into bins
        volume_bins = np.zeros(bin_count, dtype=np.float64)

        for candle in candles:
            if candle.volume <= 0 or candle.high <= candle.low:
                continue

            # Map candle range to bins
            low_bin = max(0, int((candle.low - price_low) / bin_size))
            high_bin = min(bin_count - 1, int((candle.high - price_low) / bin_size))

            if high_bin < low_bin:
                continue

            # Distribute volume uniformly across covered bins
            n_bins = high_bin - low_bin + 1
            vol_per_bin = candle.volume / n_bins
            volume_bins[low_bin:high_bin + 1] += vol_per_bin

        total_volume = float(np.sum(volume_bins))
        if total_volume <= 0:
            return None

        # POC: bin with maximum volume
        poc_idx = int(np.argmax(volume_bins))
        poc_price = price_low + (poc_idx + 0.5) * bin_size

        # Value Area: expand from POC until 70% of volume is enclosed
        va_volume_target = total_volume * self._value_area_pct
        va_low_idx = poc_idx
        va_high_idx = poc_idx
        va_volume = float(volume_bins[poc_idx])

        while va_volume < va_volume_target and (va_low_idx > 0 or va_high_idx < bin_count - 1):
            # Look one bin below and one above, add whichever has more volume
            vol_below = float(volume_bins[va_low_idx - 1]) if va_low_idx > 0 else 0
            vol_above = float(volume_bins[va_high_idx + 1]) if va_high_idx < bin_count - 1 else 0

            if vol_below >= vol_above and va_low_idx > 0:
                va_low_idx -= 1
                va_volume += vol_below
            elif va_high_idx < bin_count - 1:
                va_high_idx += 1
                va_volume += vol_above
            elif va_low_idx > 0:
                va_low_idx -= 1
                va_volume += vol_below
            else:
                break

        val = price_low + va_low_idx * bin_size
        vah = price_low + (va_high_idx + 1) * bin_size

        # HVN: bins with volume > hvn_threshold × median
        median_vol = float(np.median(volume_bins[volume_bins > 0])) if np.any(volume_bins > 0) else 0
        hvn_min = median_vol * self._hvn_threshold
        lvn_max = median_vol * self._lvn_threshold

        high_volume_nodes = []
        for i in range(bin_count):
            if volume_bins[i] >= hvn_min:
                hvn_price = price_low + (i + 0.5) * bin_size
                high_volume_nodes.append((hvn_price, float(volume_bins[i])))

        # Sort HVNs by volume descending, keep top 10
        high_volume_nodes.sort(key=lambda x: x[1], reverse=True)
        high_volume_nodes = high_volume_nodes[:10]

        # LVN: contiguous regions below lvn_max
        low_volume_nodes = []
        in_lvn = False
        lvn_start = 0
        for i in range(bin_count):
            if volume_bins[i] < lvn_max:
                if not in_lvn:
                    in_lvn = True
                    lvn_start = i
            else:
                if in_lvn:
                    # End of LVN region — must be at least 2 bins wide
                    if i - lvn_start >= 2:
                        lvn_low = price_low + lvn_start * bin_size
                        lvn_high = price_low + i * bin_size
                        low_volume_nodes.append((lvn_low, lvn_high))
                    in_lvn = False
        # Close trailing LVN
        if in_lvn and bin_count - lvn_start >= 2:
            lvn_low = price_low + lvn_start * bin_size
            lvn_high = price_low + bin_count * bin_size
            low_volume_nodes.append((lvn_low, lvn_high))

        return VolumeProfile(
            poc_price=poc_price,
            vah=vah,
            val=val,
            high_volume_nodes=high_volume_nodes,
            low_volume_nodes=low_volume_nodes,
            total_volume=total_volume,
            price_low=price_low,
            price_high=price_high,
            bin_size=bin_size,
            computed_at=candles[-1].timestamp,
        )
