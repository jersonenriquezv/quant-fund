"""
Order Block (OB) Detection, Freshness, and Mitigation.

An Order Block is the last opposing candle before an impulse move
that breaks structure:
- Bullish OB: last RED candle before a bullish break
- Bearish OB: last GREEN candle before a bearish break

Entry: 50% of OB candle body (midpoint — balances fill rate vs risk).
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
    entry_price: float          # 50% of body (midpoint)
    volume: float               # OB candle volume
    volume_ratio: float         # OB volume / average volume
    mitigated: bool             # True if price closed through full OB
    associated_break: StructureBreak  # The structure break this OB is tied to
    break_timestamp: int = 0    # Timestamp of the candle that broke structure (for mitigation guard)
    impulse_score: float = 0.0  # 0-1, strength of post-OB displacement (volume + price move)
    retest_count: int = 0       # How many times price has wicked into the OB zone without mitigating


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

        # Update retest counts for active OBs
        self._count_retests(self._active_obs[key], candles)

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
                    ob = self._create_ob(c, brk, pair, timeframe, avg_volume, candles, i)
                    if ob is not None:
                        return ob
            else:
                # Looking for last GREEN candle (close >= open)
                if c.close >= c.open:
                    ob = self._create_ob(c, brk, pair, timeframe, avg_volume, candles, i)
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
        candles: list[Candle] | None = None,
        ob_index: int = -1,
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
        # Entry at 50% of body (midpoint — deeper retrace = better R:R).
        # For bullish OB: 50% from bottom = body_low + 0.50 * range
        # For bearish OB: 50% from top = body_high - 0.50 * range
        body_range = body_high - body_low
        if brk.direction == "bullish":
            entry_price = body_low + body_range * 0.50
        else:
            entry_price = body_high - body_range * 0.50

        # Compute impulse score from candles after the OB
        impulse = self._compute_impulse_score(
            candles, ob_index, brk, avg_volume
        ) if candles is not None and ob_index >= 0 else 0.0

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
            impulse_score=impulse,
        )

    def _compute_impulse_score(
        self,
        candles: list[Candle],
        ob_index: int,
        brk: StructureBreak,
        avg_volume: float,
    ) -> float:
        """Measure displacement strength of the impulse move after the OB candle.

        Scores 0-1 based on two components (50/50):
        - Price displacement: how far the impulse moved relative to the OB body
        - Volume intensity: avg volume of impulse candles vs overall average

        Looks at up to 5 candles from OB to the structure break (the impulse).
        """
        if ob_index < 0 or avg_volume <= 0:
            return 0.0

        # Impulse candles: from OB+1 up to (and including) the break candle
        break_idx = brk.candle_index
        start = ob_index + 1
        end = min(break_idx + 1, len(candles))
        # Cap at 5 candles to avoid dilution from long drifts
        impulse_candles = candles[start:end][:5]

        if not impulse_candles:
            return 0.0

        ob_body = abs(candles[ob_index].open - candles[ob_index].close)
        if ob_body <= 0:
            return 0.0

        # Price displacement: total move from OB close to impulse extreme
        ob_close = candles[ob_index].close
        if brk.direction == "bullish":
            extreme = max(c.high for c in impulse_candles)
            displacement = extreme - ob_close
        else:
            extreme = min(c.low for c in impulse_candles)
            displacement = ob_close - extreme

        # Normalize: 3x OB body displacement = score 1.0
        disp_score = min(max(displacement / (ob_body * 3.0), 0.0), 1.0)

        # Volume intensity: avg impulse volume vs overall avg
        impulse_avg_vol = sum(c.volume for c in impulse_candles) / len(impulse_candles)
        vol_ratio = impulse_avg_vol / avg_volume
        # 3x avg volume = score 1.0
        vol_score = min(vol_ratio / 3.0, 1.0)

        return disp_score * 0.5 + vol_score * 0.5

    def _count_retests(self, obs: list[OrderBlock],
                       candles: list[Candle]) -> None:
        """Count how many times price wicked into each OB zone without mitigating.

        A retest = candle low touches bullish OB zone (low <= body_high)
        or candle high touches bearish OB zone (high >= body_low),
        but candle does NOT close through the OB (not mitigated).
        Only counts candles after the structure break.
        """
        for ob in obs:
            if ob.mitigated:
                continue

            skip_ts = ob.break_timestamp if ob.break_timestamp else ob.timestamp
            count = 0
            for candle in candles:
                if candle.timestamp <= skip_ts:
                    continue

                if ob.direction == "bullish":
                    # Price wicked down into bullish OB zone
                    if candle.low <= ob.body_high and candle.close > ob.low:
                        count += 1
                else:
                    # Price wicked up into bearish OB zone
                    if candle.high >= ob.body_low and candle.close < ob.high:
                        count += 1

            ob.retest_count = count

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
