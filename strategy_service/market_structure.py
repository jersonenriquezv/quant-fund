"""
Market Structure Analysis — Swing Points, BOS, CHoCH, Trend.

Detects swing highs/lows, then classifies structure breaks as:
- BOS (Break of Structure): continuation in trend direction
- CHoCH (Change of Character): break opposite to trend = reversal signal

All thresholds from config.settings. Recomputed from scratch each call
(200 candles is fast, eliminates state drift).
"""

from dataclasses import dataclass
from typing import Optional

from config.settings import settings
from shared.models import Candle


@dataclass(frozen=True)
class SwingPoint:
    """A confirmed swing high or swing low."""
    timestamp: int
    price: float
    index: int          # Index in the candle array
    swing_type: str     # "high" or "low"


@dataclass(frozen=True)
class StructureBreak:
    """A BOS or CHoCH event."""
    timestamp: int
    break_type: str     # "bos" or "choch"
    direction: str      # "bullish" or "bearish"
    break_price: float  # The close that confirmed the break
    broken_level: float # The swing level that was broken
    candle_index: int   # Index of the candle that broke structure


@dataclass
class MarketStructureState:
    """Complete market structure analysis result for a pair+timeframe."""
    pair: str
    timeframe: str
    trend: str                          # "bullish", "bearish", or "undefined"
    swing_highs: list[SwingPoint]
    swing_lows: list[SwingPoint]
    structure_breaks: list[StructureBreak]
    latest_break: Optional[StructureBreak]


class MarketStructureAnalyzer:
    """Analyzes candle data to detect market structure.

    Stateless per call — analyze() recomputes everything from scratch.
    State is cached by (pair, timeframe) key for retrieval between calls.
    """

    def __init__(self):
        self._states: dict[str, MarketStructureState] = {}

    def analyze(self, candles: list[Candle], pair: str,
                timeframe: str) -> MarketStructureState:
        """Full market structure analysis from scratch.

        Args:
            candles: OHLCV candles, oldest first. Needs >= 2*SWING_LOOKBACK+1.
            pair: e.g. "BTC/USDT"
            timeframe: e.g. "15m", "4h"

        Returns:
            MarketStructureState with swings, breaks, and trend.
        """
        min_candles = 2 * settings.SWING_LOOKBACK + 1

        if len(candles) < min_candles:
            state = MarketStructureState(
                pair=pair,
                timeframe=timeframe,
                trend="undefined",
                swing_highs=[],
                swing_lows=[],
                structure_breaks=[],
                latest_break=None,
            )
            self._states[f"{pair}:{timeframe}"] = state
            return state

        swing_highs, swing_lows = self._find_swing_points(candles)
        structure_breaks = self._detect_structure_breaks(
            candles, swing_highs, swing_lows
        )
        trend = self._determine_trend(structure_breaks)

        state = MarketStructureState(
            pair=pair,
            timeframe=timeframe,
            trend=trend,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            structure_breaks=structure_breaks,
            latest_break=structure_breaks[-1] if structure_breaks else None,
        )
        self._states[f"{pair}:{timeframe}"] = state
        return state

    def get_state(self, pair: str,
                  timeframe: str) -> Optional[MarketStructureState]:
        """Get cached state from last analyze() call."""
        return self._states.get(f"{pair}:{timeframe}")

    def _find_swing_points(
        self, candles: list[Candle]
    ) -> tuple[list[SwingPoint], list[SwingPoint]]:
        """Detect swing highs and swing lows.

        Swing high: candle[i].high is the max high in the window
            [i - SWING_LOOKBACK, i + SWING_LOOKBACK].
        Swing low: candle[i].low is the min low in the same window.
        """
        lookback = settings.SWING_LOOKBACK
        highs: list[SwingPoint] = []
        lows: list[SwingPoint] = []

        for i in range(lookback, len(candles) - lookback):
            window_start = i - lookback
            window_end = i + lookback + 1  # exclusive

            # Check swing high
            is_high = True
            for j in range(window_start, window_end):
                if j != i and candles[j].high >= candles[i].high:
                    is_high = False
                    break

            if is_high:
                highs.append(SwingPoint(
                    timestamp=candles[i].timestamp,
                    price=candles[i].high,
                    index=i,
                    swing_type="high",
                ))

            # Check swing low
            is_low = True
            for j in range(window_start, window_end):
                if j != i and candles[j].low <= candles[i].low:
                    is_low = False
                    break

            if is_low:
                lows.append(SwingPoint(
                    timestamp=candles[i].timestamp,
                    price=candles[i].low,
                    index=i,
                    swing_type="low",
                ))

        return highs, lows

    def _detect_structure_breaks(
        self,
        candles: list[Candle],
        swing_highs: list[SwingPoint],
        swing_lows: list[SwingPoint],
    ) -> list[StructureBreak]:
        """Detect BOS and CHoCH events.

        Walk candles forward. For each candle, check if close exceeds
        any previous swing level by BOS_CONFIRMATION_PCT.
        Wick-only breaks are NOT counted — must be candle close.

        Same direction as current trend = BOS.
        Opposite direction = CHoCH.

        Only the most significant break per candle is kept (largest
        distance from the broken level) to avoid noise from large candles
        breaking multiple levels.
        """
        if not swing_highs and not swing_lows:
            return []

        breaks: list[StructureBreak] = []
        current_trend = "undefined"
        threshold_pct = settings.BOS_CONFIRMATION_PCT

        # Track which swing levels have been broken to avoid duplicate breaks
        broken_high_indices: set[int] = set()
        broken_low_indices: set[int] = set()

        for i, candle in enumerate(candles):
            # Collect all candidate breaks for this candle
            candidates: list[tuple[StructureBreak, int, str]] = []
            # Each tuple: (break, swing_index, "high" or "low")

            # Check bullish breaks — close above swing high
            for h_idx in range(len(swing_highs)):
                sh = swing_highs[h_idx]
                if sh.index >= i:
                    break  # Only check swings that formed before this candle
                if h_idx in broken_high_indices:
                    continue

                required = sh.price * (1 + threshold_pct)
                if candle.close >= required:
                    if current_trend == "bullish" or current_trend == "undefined":
                        break_type = "bos"
                    else:
                        break_type = "choch"

                    candidates.append((
                        StructureBreak(
                            timestamp=candle.timestamp,
                            break_type=break_type,
                            direction="bullish",
                            break_price=candle.close,
                            broken_level=sh.price,
                            candle_index=i,
                        ),
                        h_idx,
                        "high",
                    ))

            # Check bearish breaks — close below swing low
            for l_idx in range(len(swing_lows)):
                sl = swing_lows[l_idx]
                if sl.index >= i:
                    break  # Only check swings that formed before this candle
                if l_idx in broken_low_indices:
                    continue

                required = sl.price * (1 - threshold_pct)
                if candle.close <= required:
                    if current_trend == "bearish" or current_trend == "undefined":
                        break_type = "bos"
                    else:
                        break_type = "choch"

                    candidates.append((
                        StructureBreak(
                            timestamp=candle.timestamp,
                            break_type=break_type,
                            direction="bearish",
                            break_price=candle.close,
                            broken_level=sl.price,
                            candle_index=i,
                        ),
                        l_idx,
                        "low",
                    ))

            if not candidates:
                continue

            # Keep only the most significant break for this candle
            # (largest distance from the broken level)
            best_brk, best_idx, best_type = max(
                candidates,
                key=lambda c: abs(c[0].break_price - c[0].broken_level),
            )

            breaks.append(best_brk)
            if best_type == "high":
                broken_high_indices.add(best_idx)
            else:
                broken_low_indices.add(best_idx)
            current_trend = best_brk.direction

            # Mark all other broken levels from this candle as consumed
            # so they don't trigger again on future candles
            for brk, idx, typ in candidates:
                if typ == "high":
                    broken_high_indices.add(idx)
                else:
                    broken_low_indices.add(idx)

        return breaks

    def _determine_trend(self, breaks: list[StructureBreak]) -> str:
        """Determine current trend from structure breaks.

        Latest break determines the trend direction.
        No breaks = undefined.
        """
        if not breaks:
            return "undefined"
        return breaks[-1].direction
