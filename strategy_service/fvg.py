"""
Fair Value Gap (FVG) Detection.

A 3-candle pattern where the wick of candle 1 does not overlap with
the wick of candle 3, creating an imbalance zone.

- Bullish FVG: candle1.high < candle3.low (gap up)
- Bearish FVG: candle1.low > candle3.high (gap down)
- Minimum size: FVG_MIN_SIZE_PCT of price
- Expiration: FVG_MAX_AGE_HOURS
- Fully filled FVGs are pruned
"""

from dataclasses import dataclass

from config.settings import settings
from shared.models import Candle


@dataclass
class FairValueGap:
    """A detected fair value gap."""
    timestamp: int          # Timestamp of the middle candle (candle 2)
    pair: str
    timeframe: str
    direction: str          # "bullish" or "bearish"
    high: float             # Upper bound of the gap
    low: float              # Lower bound of the gap
    size_pct: float         # Gap size as % of price
    filled_pct: float       # How much of the gap has been filled (0.0 - 1.0)
    fully_filled: bool      # True if price has completely closed the gap


class FVGDetector:
    """Detects and tracks Fair Value Gaps.

    State persists between calls — active FVGs survive multiple candle cycles.
    Expired and fully filled FVGs are pruned on each update.
    """

    def __init__(self):
        # Key: "pair:timeframe", Value: list of active FVGs
        self._active_fvgs: dict[str, list[FairValueGap]] = {}

    def update(self, candles: list[Candle], pair: str, timeframe: str,
               current_time_ms: int,
               max_age_hours: int | None = None) -> list[FairValueGap]:
        """Detect new FVGs and update fill status of existing ones.

        Args:
            candles: OHLCV candles, oldest first. Needs >= 3.
            pair: e.g. "BTC/USDT"
            timeframe: e.g. "15m"
            current_time_ms: Current time in milliseconds for expiration check.
            max_age_hours: Override for FVG_MAX_AGE_HOURS (used by HTF campaigns).

        Returns:
            List of currently active (non-expired, non-filled) FVGs.
        """
        key = f"{pair}:{timeframe}"

        if key not in self._active_fvgs:
            self._active_fvgs[key] = []

        # Detect new FVGs
        new_fvgs = self._detect_fvgs(candles, pair, timeframe)

        # Deduplicate — don't add FVGs we already track
        existing_keys = {
            (fvg.timestamp, fvg.direction) for fvg in self._active_fvgs[key]
        }
        for fvg in new_fvgs:
            if (fvg.timestamp, fvg.direction) not in existing_keys:
                self._active_fvgs[key].append(fvg)

        # Update fill status with latest candles
        self._update_fill_status(self._active_fvgs[key], candles)

        # Prune expired and fully filled
        age_hours = max_age_hours if max_age_hours is not None else settings.FVG_MAX_AGE_HOURS
        max_age_ms = age_hours * 3600 * 1000
        self._active_fvgs[key] = [
            fvg for fvg in self._active_fvgs[key]
            if not fvg.fully_filled
            and (current_time_ms - fvg.timestamp) <= max_age_ms
        ]

        return list(self._active_fvgs[key])

    def get_active_fvgs(self, pair: str,
                        timeframe: str) -> list[FairValueGap]:
        """Get currently active FVGs for a pair+timeframe."""
        return list(self._active_fvgs.get(f"{pair}:{timeframe}", []))

    def _detect_fvgs(self, candles: list[Candle], pair: str,
                     timeframe: str) -> list[FairValueGap]:
        """Scan candles for 3-candle FVG patterns.

        Bullish FVG: candle1.high < candle3.low (gap between wicks)
        Bearish FVG: candle1.low > candle3.high (gap between wicks)
        """
        fvgs: list[FairValueGap] = []

        if len(candles) < 3:
            return fvgs

        min_size_pct = settings.FVG_MIN_SIZE_PCT

        for i in range(len(candles) - 2):
            c1 = candles[i]
            c2 = candles[i + 1]
            c3 = candles[i + 2]

            # Bullish FVG: gap up — candle1 high doesn't reach candle3 low
            if c1.high < c3.low:
                gap_low = c1.high
                gap_high = c3.low
                mid_price = c2.close if c2.close > 0 else c2.high
                size_pct = (gap_high - gap_low) / mid_price if mid_price > 0 else 0

                if size_pct >= min_size_pct:
                    fvgs.append(FairValueGap(
                        timestamp=c2.timestamp,
                        pair=pair,
                        timeframe=timeframe,
                        direction="bullish",
                        high=gap_high,
                        low=gap_low,
                        size_pct=size_pct,
                        filled_pct=0.0,
                        fully_filled=False,
                    ))

            # Bearish FVG: gap down — candle1 low doesn't reach candle3 high
            if c1.low > c3.high:
                gap_high = c1.low
                gap_low = c3.high
                mid_price = c2.close if c2.close > 0 else c2.low
                size_pct = (gap_high - gap_low) / mid_price if mid_price > 0 else 0

                if size_pct >= min_size_pct:
                    fvgs.append(FairValueGap(
                        timestamp=c2.timestamp,
                        pair=pair,
                        timeframe=timeframe,
                        direction="bearish",
                        high=gap_high,
                        low=gap_low,
                        size_pct=size_pct,
                        filled_pct=0.0,
                        fully_filled=False,
                    ))

        return fvgs

    def _update_fill_status(self, fvgs: list[FairValueGap],
                            candles: list[Candle]) -> None:
        """Update how much of each FVG has been filled by subsequent price action.

        For bullish FVG: price coming down into the gap fills it.
        For bearish FVG: price coming up into the gap fills it.
        """
        for fvg in fvgs:
            if fvg.fully_filled:
                continue

            gap_size = fvg.high - fvg.low
            if gap_size <= 0:
                fvg.fully_filled = True
                fvg.filled_pct = 1.0
                continue

            max_fill = 0.0

            for candle in candles:
                # Only check candles after the FVG formed
                if candle.timestamp <= fvg.timestamp:
                    continue

                if fvg.direction == "bullish":
                    # Price coming down fills bullish FVG
                    if candle.low <= fvg.high:
                        penetration = fvg.high - max(candle.low, fvg.low)
                        fill = penetration / gap_size
                        max_fill = max(max_fill, fill)
                else:
                    # Price coming up fills bearish FVG
                    if candle.high >= fvg.low:
                        penetration = min(candle.high, fvg.high) - fvg.low
                        fill = penetration / gap_size
                        max_fill = max(max_fill, fill)

            fvg.filled_pct = min(max_fill, 1.0)
            if fvg.filled_pct >= 1.0:
                fvg.fully_filled = True
