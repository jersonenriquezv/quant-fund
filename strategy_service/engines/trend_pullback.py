"""Engine 1 — Trend-Pullback / Impulse Retest.

See `docs/strategy_redesign_2026_04.md` §4.1 for the thesis. Short
version: when price has just made a strong directional impulse
(measurable as multi-bar displacement above ATR with high directional
purity) and then retraces a controlled pullback that does NOT invalidate
the impulse origin, the next move tends to be continuation.

This engine is shadow-only and BTC/ETH-only at first ship. It does NOT
reuse setups.py's `_apply_expectancy_filters`; its entry-distance and
target-space rules are part of the engine contract (otherwise we
rebuild Setup F with a different name).

v1 thresholds below are heuristic and documented as such — they are
NOT optimized parameters. Calibration will follow once a sample of
≥50 resolved outcomes accumulates.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from config.settings import settings
from shared.models import Candle, TradeSetup
from shared.logger import setup_logger

logger = setup_logger("engine1_trend_pullback")


SETUP_TYPE = "engine1_trend_pullback"

# --- v1 heuristic thresholds (documented, not optimized) ---

# Impulse leg: multi-bar directional move
IMPULSE_MIN_CANDLES = 3
IMPULSE_MAX_CANDLES = 8
IMPULSE_MIN_ATR_MULT = 2.0
IMPULSE_MIN_BODY_RATIO = 0.55
IMPULSE_MIN_DIRECTIONAL = 0.6  # fraction of candles in net direction

# Pullback: controlled retracement
PULLBACK_MIN_CANDLES = 2
PULLBACK_MAX_CANDLES = 6
PULLBACK_MIN_RETRACE = 0.30
PULLBACK_MAX_RETRACE = 0.85   # must NOT break impulse origin (=1.0)
PULLBACK_MAX_OPPOSING_BODY_RATIO = 0.7

# Entry zone: Fibonacci retracement of impulse, biased to 50%
ENTRY_FIB_TARGET = 0.50
ENTRY_MAX_ATR_MULT = 1.5  # entry must be ≤ this × ATR from current price

# SL: beyond impulse origin with small buffer; floor at ATR multiple
SL_BUFFER_MULT = 1.05  # SL at impulse_origin ± 5% × impulse_distance
SL_ATR_FLOOR_MULT = 1.0  # SL distance must be ≥ this × ATR

# TP: TP1 at fixed R:R for partial; TP2 takes the more conservative of
# fixed-R:R and structural-swing-with-clear-room.
TP1_RR = 1.0
TP2_RR_FIXED = 2.0
TP2_MIN_RR_NET = 1.6  # required after fees+slippage buffer
FEE_RR_BUFFER = 0.2   # deducted from gross R:R as fee+slippage estimate
TP2_TARGET_SPACE_R = 1.4  # nearest opposing swing must leave ≥ this × risk
                          # of clear room beyond TP2


@dataclass(frozen=True)
class ImpulseLeg:
    """A multi-bar directional move that may anchor an impulse-retest setup."""
    start_idx: int           # inclusive, first candle of impulse
    end_idx: int             # inclusive, last candle of impulse
    direction: str           # "long" or "short"
    origin_price: float      # opposite extreme of starting candle
    peak_price: float        # extreme of ending candle
    displacement_pct: float  # |peak - origin| / origin
    atr_multiple: float      # |peak - origin| / atr
    avg_body_ratio: float    # mean(|body| / range) over impulse candles
    candle_count: int


@dataclass(frozen=True)
class Pullback:
    """A controlled retracement after an impulse leg."""
    start_idx: int                  # inclusive, first pullback candle
    end_idx: int                    # inclusive, last (most recent) candle
    depth_pct: float                # retrace fraction of impulse
    atr_multiple: float             # pullback range / ATR
    candle_count: int
    max_opposing_body_ratio: float  # largest single in-impulse-direction body
                                    # within pullback / pullback range


# ============================================================
# Pure helpers — no side effects, easily tested in isolation
# ============================================================


def compute_atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """Average True Range over `period` candles ending at the last candle."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        c = candles[i]
        prev = candles[i - 1]
        tr = max(
            c.high - c.low,
            abs(c.high - prev.close),
            abs(c.low - prev.close),
        )
        trs.append(tr)
    return sum(trs) / period


def _qualify_impulse(
    impulse_candles: Sequence[Candle],
    start_idx: int,
    atr: float,
) -> Optional[ImpulseLeg]:
    """Check if a candle window qualifies as an impulse leg."""
    n = len(impulse_candles)
    if n < IMPULSE_MIN_CANDLES or atr <= 0:
        return None

    first = impulse_candles[0]
    last = impulse_candles[-1]
    net_close_delta = last.close - first.close
    if net_close_delta == 0:
        return None

    direction = "long" if net_close_delta > 0 else "short"

    # Directional purity: fraction of candles whose body sign matches the net.
    aligned = sum(
        1 for c in impulse_candles
        if (c.close - c.open) * net_close_delta > 0
    )
    if aligned / n < IMPULSE_MIN_DIRECTIONAL:
        return None

    # Body / range ratio averaged over the leg.
    body_ratios = []
    for c in impulse_candles:
        rng = c.high - c.low
        if rng > 0:
            body_ratios.append(abs(c.close - c.open) / rng)
    if not body_ratios:
        return None
    avg_body_ratio = sum(body_ratios) / len(body_ratios)
    if avg_body_ratio < IMPULSE_MIN_BODY_RATIO:
        return None

    # Origin = opposite extreme of first candle in the leg's direction.
    if direction == "long":
        origin = first.low
        peak = last.high
    else:
        origin = first.high
        peak = last.low

    displacement = abs(peak - origin)
    if origin <= 0:
        return None
    displacement_pct = displacement / origin
    atr_multiple = displacement / atr
    if atr_multiple < IMPULSE_MIN_ATR_MULT:
        return None

    return ImpulseLeg(
        start_idx=start_idx,
        end_idx=start_idx + n - 1,
        direction=direction,
        origin_price=origin,
        peak_price=peak,
        displacement_pct=displacement_pct,
        atr_multiple=atr_multiple,
        avg_body_ratio=avg_body_ratio,
        candle_count=n,
    )


def _qualify_pullback(
    pullback_candles: Sequence[Candle],
    start_idx: int,
    impulse: ImpulseLeg,
    atr: float,
) -> Optional[Pullback]:
    """Check if a window after the impulse is a clean pullback."""
    n = len(pullback_candles)
    if n < PULLBACK_MIN_CANDLES or atr <= 0:
        return None

    impulse_distance = abs(impulse.peak_price - impulse.origin_price)
    if impulse_distance <= 0:
        return None

    # Pullback extreme = furthest retrace within the window.
    if impulse.direction == "long":
        pullback_extreme = min(c.low for c in pullback_candles)
        pullback_range = impulse.peak_price - pullback_extreme
    else:
        pullback_extreme = max(c.high for c in pullback_candles)
        pullback_range = pullback_extreme - impulse.peak_price

    if pullback_range <= 0:
        return None

    depth_pct = pullback_range / impulse_distance
    if not (PULLBACK_MIN_RETRACE <= depth_pct <= PULLBACK_MAX_RETRACE):
        return None

    # Opposing-direction body domination: a single candle going IN the
    # impulse direction (= against the pullback) larger than the threshold
    # × pullback range disqualifies the pattern (pullback isn't clean).
    max_opp_body = 0.0
    for c in pullback_candles:
        body = c.close - c.open  # signed
        if impulse.direction == "long" and body > 0:
            max_opp_body = max(max_opp_body, body)
        elif impulse.direction == "short" and body < 0:
            max_opp_body = max(max_opp_body, abs(body))
    max_opposing_body_ratio = max_opp_body / pullback_range
    if max_opposing_body_ratio > PULLBACK_MAX_OPPOSING_BODY_RATIO:
        return None

    return Pullback(
        start_idx=start_idx,
        end_idx=start_idx + n - 1,
        depth_pct=depth_pct,
        atr_multiple=pullback_range / atr,
        candle_count=n,
        max_opposing_body_ratio=max_opposing_body_ratio,
    )


def detect_impulse_pullback(
    candles: Sequence[Candle],
    atr: float,
) -> Optional[tuple[ImpulseLeg, Pullback]]:
    """Search for the most recent valid (impulse, pullback) pair.

    Pattern: [...history] [IMPULSE leg] [PULLBACK leg] (latest candle).
    The pullback ends at `candles[-1]`. The impulse ends right before the
    pullback starts. We iterate over (P, I) combinations from smallest
    pullback first to find the freshest qualifying pair.

    Returns (impulse, pullback) or None.
    """
    n = len(candles)
    min_history = PULLBACK_MIN_CANDLES + IMPULSE_MIN_CANDLES
    if n < min_history or atr <= 0:
        return None

    for P in range(PULLBACK_MIN_CANDLES, PULLBACK_MAX_CANDLES + 1):
        for I in range(IMPULSE_MIN_CANDLES, IMPULSE_MAX_CANDLES + 1):
            if P + I > n:
                continue
            imp_start = n - P - I
            pull_start = n - P
            imp_window = candles[imp_start:pull_start]
            pull_window = candles[pull_start:]

            impulse = _qualify_impulse(imp_window, imp_start, atr)
            if impulse is None:
                continue
            pullback = _qualify_pullback(pull_window, pull_start, impulse, atr)
            if pullback is None:
                continue
            return impulse, pullback
    return None


def compute_entry(impulse: ImpulseLeg) -> float:
    """Limit-order entry at the Fibonacci target retracement of the impulse."""
    impulse_distance = abs(impulse.peak_price - impulse.origin_price)
    if impulse.direction == "long":
        return impulse.peak_price - impulse_distance * ENTRY_FIB_TARGET
    return impulse.peak_price + impulse_distance * ENTRY_FIB_TARGET


def compute_sl(impulse: ImpulseLeg, entry: float, atr: float) -> float:
    """SL = beyond impulse origin (5% buffer past), floored at 1× ATR.

    Hard invalidation = a close past the impulse origin. The buffer
    leaves a small wick allowance. The ATR floor prevents structurally
    tight SLs in low-volatility regimes.
    """
    impulse_distance = abs(impulse.peak_price - impulse.origin_price)
    buffer = impulse_distance * (SL_BUFFER_MULT - 1.0)
    if impulse.direction == "long":
        structural_sl = impulse.origin_price - buffer
        atr_floor_sl = entry - atr * SL_ATR_FLOOR_MULT
        return min(structural_sl, atr_floor_sl)
    structural_sl = impulse.origin_price + buffer
    atr_floor_sl = entry + atr * SL_ATR_FLOOR_MULT
    return max(structural_sl, atr_floor_sl)


def compute_tp(
    entry: float,
    sl: float,
    direction: str,
    swings_htf: Sequence[float],
) -> Optional[tuple[float, float]]:
    """Compute (TP1, TP2). Returns None if no level yields acceptable net R:R.

    TP1 = TP1_RR × risk (partial-exit anchor).
    TP2 = the more conservative of TP2_RR_FIXED × risk and the nearest
    opposing HTF swing that leaves ≥ TP2_TARGET_SPACE_R × risk of clean
    room beyond it. The chosen TP2 must yield ≥ TP2_MIN_RR_NET after the
    fee/slippage buffer is deducted.
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return None

    if direction == "long":
        tp1 = entry + risk * TP1_RR
        tp2_fixed = entry + risk * TP2_RR_FIXED
    else:
        tp1 = entry - risk * TP1_RR
        tp2_fixed = entry - risk * TP2_RR_FIXED

    # Pick TP2: prefer the closer of (fixed) vs (next opposing swing
    # that has TP2_TARGET_SPACE_R room beyond it).
    tp2_structural = None
    if swings_htf:
        if direction == "long":
            beyond = [s for s in swings_htf if s > tp1]
            beyond.sort()
            for level in beyond:
                # Need next swing beyond `level` ≥ TP2_TARGET_SPACE_R × risk
                farther = [s for s in beyond if s > level]
                if not farther or (farther[0] - level) >= risk * TP2_TARGET_SPACE_R:
                    tp2_structural = level
                    break
        else:
            beyond = [s for s in swings_htf if s < tp1]
            beyond.sort(reverse=True)
            for level in beyond:
                farther = [s for s in beyond if s < level]
                if not farther or (level - farther[0]) >= risk * TP2_TARGET_SPACE_R:
                    tp2_structural = level
                    break

    if tp2_structural is None:
        tp2 = tp2_fixed
    elif direction == "long":
        tp2 = min(tp2_fixed, tp2_structural)
    else:
        tp2 = max(tp2_fixed, tp2_structural)

    gross_rr = abs(tp2 - entry) / risk
    net_rr = gross_rr - FEE_RR_BUFFER
    if net_rr < TP2_MIN_RR_NET:
        return None

    return tp1, tp2


def is_entry_within_atr(
    entry: float,
    current_price: float,
    atr: float,
) -> bool:
    """Entry-distance gate. Engine 1's own — not the legacy expectancy filter."""
    if atr <= 0 or current_price <= 0:
        return False
    return abs(current_price - entry) <= atr * ENTRY_MAX_ATR_MULT


# ============================================================
# Engine class — wraps helpers + builds TradeSetup
# ============================================================


class TrendPullbackEngine:
    """Stateless engine. One instance per StrategyService is fine."""

    def evaluate(
        self,
        *,
        pair: str,
        candles: Sequence[Candle],
        current_price: float,
        htf_bias: str,
        swings_htf: Sequence[float],
        ob_timeframe: str = "15m",
    ) -> Optional[TradeSetup]:
        """Detect impulse + pullback and emit a `TradeSetup` if all gates pass.

        Returns None on any rejection. The reasons are debug-logged; not
        all rejections are noisy because shadow data collection should not
        spam INFO when no setup exists.
        """
        if not htf_bias or htf_bias == "undefined":
            return None
        if not candles:
            return None

        atr = compute_atr(candles, period=14)
        if atr is None or atr <= 0:
            return None

        result = detect_impulse_pullback(candles, atr)
        if result is None:
            return None
        impulse, pullback = result

        # Direction must align with HTF bias for trend-continuation thesis.
        expected_dir = "long" if htf_bias == "bullish" else "short"
        if impulse.direction != expected_dir:
            logger.debug(
                f"Engine1 [{pair}]: impulse dir {impulse.direction} != HTF {expected_dir}"
            )
            return None

        entry = compute_entry(impulse)
        if not is_entry_within_atr(entry, current_price, atr):
            logger.debug(
                f"Engine1 [{pair}]: entry {entry:.4f} > {ENTRY_MAX_ATR_MULT}×ATR "
                f"from price {current_price:.4f}"
            )
            return None

        sl = compute_sl(impulse, entry, atr)
        tp = compute_tp(entry, sl, impulse.direction, swings_htf)
        if tp is None:
            logger.debug(f"Engine1 [{pair}]: TP rejected (insufficient net R:R)")
            return None
        tp1, tp2 = tp

        # Sanity: SL on correct side of entry.
        if impulse.direction == "long" and sl >= entry:
            return None
        if impulse.direction == "short" and sl <= entry:
            return None

        confluences = [
            f"engine1_impulse_atr_{impulse.atr_multiple:.2f}x",
            f"engine1_impulse_body_{impulse.avg_body_ratio:.2f}",
            f"engine1_impulse_candles_{impulse.candle_count}",
            f"engine1_impulse_displacement_{impulse.displacement_pct*100:.2f}pct",
            f"engine1_pullback_depth_{pullback.depth_pct*100:.1f}pct",
            f"engine1_pullback_atr_{pullback.atr_multiple:.2f}x",
            f"engine1_pullback_candles_{pullback.candle_count}",
            f"engine1_pullback_max_opp_{pullback.max_opposing_body_ratio:.2f}",
        ]

        # Lossless raw metrics for ML — confluence strings above are
        # formatted/truncated and lose precision. Full set kept here so
        # future audits can recover the exact decision inputs.
        entry_atr_distance = abs(entry - current_price) / atr if atr > 0 else 0.0
        extra_features: dict[str, int | float | str | bool | None] = {
            "engine1_impulse_atr_multiple": float(impulse.atr_multiple),
            "engine1_impulse_body_ratio": float(impulse.avg_body_ratio),
            "engine1_impulse_candle_count": int(impulse.candle_count),
            "engine1_impulse_displacement_pct": float(impulse.displacement_pct),
            "engine1_pullback_depth_pct": float(pullback.depth_pct),
            "engine1_pullback_atr_multiple": float(pullback.atr_multiple),
            "engine1_pullback_candle_count": int(pullback.candle_count),
            "engine1_pullback_max_opposing_body_ratio": float(
                pullback.max_opposing_body_ratio
            ),
            "engine1_entry_atr_distance": float(entry_atr_distance),
        }

        last_candle = candles[-1]
        return TradeSetup(
            timestamp=last_candle.timestamp,
            pair=pair,
            direction=impulse.direction,
            setup_type=SETUP_TYPE,
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=ob_timeframe,
            extra_features=extra_features,
        )
