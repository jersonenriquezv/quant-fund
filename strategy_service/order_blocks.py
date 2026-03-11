"""
Order Block (OB) Detection, Freshness, and Mitigation.

An Order Block is the last opposing candle before an impulse move
that breaks structure:
- Bullish OB: last RED candle before a bullish break
- Bearish OB: last GREEN candle before a bearish break

Entry: 75% of OB candle body (closer to price for higher fill rate).
SL: Below/above entire OB (wick-to-wick).
Volume filter: OB volume must be >= OB_MIN_VOLUME_RATIO * average.
Freshness: Max OB_MAX_AGE_HOURS. Price closing through full OB = mitigated.
"""

from dataclasses import dataclass
from typing import Optional

from config.settings import settings
from shared.models import Candle
from strategy_service.market_structure import StructureBreak


@dataclass
class OrderBlock:
    """A detected order block zone."""
    timestamp: int              # Timestamp of the OB candle
    pair: str
    timeframe: str
    direction: str              # "bullish" or "bearish"
    high: float                 # OB candle high (wick)
    low: float                  # OB candle low (wick)
    body_high: float            # max(open, close) of OB candle
    body_low: float             # min(open, close) of OB candle
    entry_price: float          # 75% of body (closer to price action)
    volume: float               # OB candle volume
    volume_ratio: float         # OB volume / average volume
    mitigated: bool             # True if price closed through full OB
    associated_break: StructureBreak  # The structure break this OB is tied to
    break_timestamp: int = 0    # Timestamp of the candle that broke structure (for mitigation guard)


class OrderBlockDetector:
    """Detects and tracks Order Blocks.

    State persists between calls — OBs survive multiple candle cycles.
    Mitigated and expired OBs are pruned on each update.
    """

    def __init__(self):
        # Key: "pair:timeframe", Value: list of active OBs
        self._active_obs: dict[str, list[OrderBlock]] = {}
        # Key: "pair:timeframe", Value: list of breaker blocks (mitigated OBs with flipped direction)
        self._breaker_blocks: dict[str, list[OrderBlock]] = {}

    def update(self, candles: list[Candle],
               structure_breaks: list[StructureBreak],
               pair: str, timeframe: str,
               current_time_ms: int,
               max_age_hours: int | None = None) -> list[OrderBlock]:
        """Detect new OBs from structure breaks and update existing ones.

        Args:
            candles: OHLCV candles, oldest first.
            structure_breaks: Detected BOS/CHoCH events from MarketStructureAnalyzer.
            pair: e.g. "BTC/USDT"
            timeframe: e.g. "15m"
            current_time_ms: Current time in milliseconds.
            max_age_hours: Override for OB_MAX_AGE_HOURS (used by HTF campaigns).

        Returns:
            List of currently active (non-mitigated, non-expired) OBs.
        """
        key = f"{pair}:{timeframe}"

        if key not in self._active_obs:
            self._active_obs[key] = []

        # Detect new OBs from structure breaks
        avg_volume = self._compute_avg_volume(candles)

        existing_timestamps = {
            ob.timestamp for ob in self._active_obs[key]
        }

        for brk in structure_breaks:
            ob = self._find_ob_candle(candles, brk, pair, timeframe, avg_volume)
            if ob is not None and ob.timestamp not in existing_timestamps:
                self._active_obs[key].append(ob)
                existing_timestamps.add(ob.timestamp)

        # Update mitigation status and capture new breaker blocks
        if key not in self._breaker_blocks:
            self._breaker_blocks[key] = []

        new_breakers = self._check_mitigation(self._active_obs[key], candles)

        # Deduplicate breaker blocks by timestamp
        existing_bb_ts = {bb.timestamp for bb in self._breaker_blocks[key]}
        for bb in new_breakers:
            if bb.timestamp not in existing_bb_ts:
                self._breaker_blocks[key].append(bb)
                existing_bb_ts.add(bb.timestamp)

        # Prune mitigated and expired OBs
        age_hours = max_age_hours if max_age_hours is not None else settings.OB_MAX_AGE_HOURS
        max_age_ms = age_hours * 3600 * 1000
        self._active_obs[key] = [
            ob for ob in self._active_obs[key]
            if not ob.mitigated
            and not self._is_expired(ob, current_time_ms, max_age_ms)
        ]

        # Prune expired breaker blocks
        self._breaker_blocks[key] = [
            bb for bb in self._breaker_blocks[key]
            if not self._is_expired(bb, current_time_ms, max_age_ms)
        ]

        return list(self._active_obs[key])

    def get_active_obs(self, pair: str,
                       timeframe: str) -> list[OrderBlock]:
        """Get currently active OBs for a pair+timeframe."""
        return list(self._active_obs.get(f"{pair}:{timeframe}", []))

    def get_breaker_blocks(self, pair: str,
                           timeframe: str) -> list[OrderBlock]:
        """Get breaker blocks (mitigated OBs with flipped direction) for a pair+timeframe."""
        return list(self._breaker_blocks.get(f"{pair}:{timeframe}", []))

    def _find_ob_candle(
        self,
        candles: list[Candle],
        brk: StructureBreak,
        pair: str,
        timeframe: str,
        avg_volume: float,
    ) -> Optional[OrderBlock]:
        """Find the OB candle for a given structure break.

        Scan backwards from the break candle:
        - Bullish break → last RED candle (close < open)
        - Bearish break → last GREEN candle (close >= open)
        Max 10 candles back.
        """
        break_idx = brk.candle_index
        max_lookback = 10

        start_idx = max(0, break_idx - max_lookback)

        for i in range(break_idx - 1, start_idx - 1, -1):
            if i < 0:
                break

            c = candles[i]

            if brk.direction == "bullish":
                # Looking for last RED candle (close < open)
                if c.close < c.open:
                    ob = self._create_ob(c, brk, pair, timeframe, avg_volume)
                    if ob is not None:
                        return ob
            else:
                # Looking for last GREEN candle (close >= open)
                if c.close >= c.open:
                    ob = self._create_ob(c, brk, pair, timeframe, avg_volume)
                    if ob is not None:
                        return ob

        return None

    def _create_ob(
        self,
        candle: Candle,
        brk: StructureBreak,
        pair: str,
        timeframe: str,
        avg_volume: float,
    ) -> Optional[OrderBlock]:
        """Create an OrderBlock from a candle, applying volume filter."""
        volume_ratio = (
            candle.volume / avg_volume if avg_volume > 0 else 0.0
        )

        # Volume filter: must be >= OB_MIN_VOLUME_RATIO
        if volume_ratio < settings.OB_MIN_VOLUME_RATIO:
            return None

        body_high = max(candle.open, candle.close)
        body_low = min(candle.open, candle.close)
        # Entry at 75% of body (closer to price action = higher fill rate).
        # For bullish OB: 75% from bottom = body_low + 0.75 * range (higher entry)
        # For bearish OB: 75% from top = body_high - 0.75 * range (lower entry)
        body_range = body_high - body_low
        if brk.direction == "bullish":
            entry_price = body_low + body_range * 0.75
        else:
            entry_price = body_high - body_range * 0.75

        return OrderBlock(
            timestamp=candle.timestamp,
            pair=pair,
            timeframe=timeframe,
            direction=brk.direction,
            high=candle.high,
            low=candle.low,
            body_high=body_high,
            body_low=body_low,
            entry_price=entry_price,
            volume=candle.volume,
            volume_ratio=volume_ratio,
            mitigated=False,
            associated_break=brk,
            break_timestamp=brk.timestamp,
        )

    def _compute_avg_volume(self, candles: list[Candle]) -> float:
        """Compute average volume over VOLUME_AVG_PERIODS candles."""
        periods = settings.VOLUME_AVG_PERIODS
        relevant = candles[-periods:] if len(candles) >= periods else candles

        if not relevant:
            return 0.0

        return sum(c.volume for c in relevant) / len(relevant)

    def _check_mitigation(self, obs: list[OrderBlock],
                          candles: list[Candle]) -> list[OrderBlock]:
        """Check if price has closed through the full OB zone.

        Bullish OB mitigated: candle closes below OB low.
        Bearish OB mitigated: candle closes above OB high.

        Returns list of new breaker blocks (mitigated OBs with flipped direction).
        """
        breakers: list[OrderBlock] = []
        for ob in obs:
            if ob.mitigated:
                continue

            # Skip candles up to the break candle — they cannot mitigate the OB
            skip_ts = ob.break_timestamp if ob.break_timestamp else ob.timestamp
            for candle in candles:
                if candle.timestamp <= skip_ts:
                    continue

                if ob.direction == "bullish" and candle.close < ob.low:
                    ob.mitigated = True
                    breakers.append(self._create_breaker(ob, candle, "bearish"))
                    break
                elif ob.direction == "bearish" and candle.close > ob.high:
                    ob.mitigated = True
                    breakers.append(self._create_breaker(ob, candle, "bullish"))
                    break
        return breakers

    def _create_breaker(self, ob: OrderBlock, mitigation_candle: Candle,
                        new_direction: str) -> OrderBlock:
        """Create a breaker block from a mitigated OB with flipped direction."""
        return OrderBlock(
            timestamp=ob.timestamp,
            pair=ob.pair,
            timeframe=ob.timeframe,
            direction=new_direction,
            high=ob.high,
            low=ob.low,
            body_high=ob.body_high,
            body_low=ob.body_low,
            entry_price=ob.entry_price,
            volume=ob.volume,
            volume_ratio=ob.volume_ratio,
            mitigated=False,
            associated_break=ob.associated_break,
            break_timestamp=mitigation_candle.timestamp,
        )

    def _is_expired(self, ob: OrderBlock, current_time_ms: int,
                    max_age_ms: int) -> bool:
        """Check if OB has exceeded max age."""
        return (current_time_ms - ob.timestamp) > max_age_ms
