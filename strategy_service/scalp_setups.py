"""
Scalp Shadow Signals — v1 experiment.

Microstructural scalping signals tested in shadow mode only. No live execution.
Plan: docs/plans/scalp_shadow_v1.md.

Five candidates (4 signals + control baseline):
- scalp_liq_reclaim_v1     — OI drop + wick reclaim
- scalp_sweep_choch_v1     — sweep of 20-bar high/low + 1m CHoCH
- scalp_vol_cvd_div_v1     — 3-sigma volume + CVD/price divergence
- scalp_funding_extreme_v1 — funding extreme + flat price last 30min
- scalp_random_baseline_v1 — random control, frequency-matched

Per-signal TP/SL/time_stop comes from settings.SCALP_SIGNAL_PARAMS.
ShadowMonitor reads time_stop_seconds from there at add_shadow time.

Detectors run on settings.SCALP_TIMEFRAME (default 5m) until 1m candle
fetching is added in a follow-up commit.
"""

import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, MarketSnapshot, TradeSetup

logger = setup_logger("strategy_scalp_setups")

# Wick reclaim threshold — minimum wick size as fraction of close price.
_LIQ_RECLAIM_WICK_THRESHOLD = 0.005  # 0.5%
# Lookback bars (excluding the trigger candle itself) for the inside-range check.
_LIQ_RECLAIM_LOOKBACK_BARS = 20
# Max age of OI flush events (ms) considered fresh enough to anchor a reclaim.
_LIQ_RECLAIM_FLUSH_MAX_AGE_MS = 5 * 60 * 1000

# Sweep + CHoCH:
# Lookback bars for prior high/low envelope (excluding sweep + confirm candles).
_SWEEP_CHOCH_LOOKBACK_BARS = 20
# Minimum body size as fraction of confirm candle range — filters noise candles
# whose body is dwarfed by their wicks.
_SWEEP_CHOCH_MIN_BODY_RATIO = 0.60


class ScalpSetupEvaluator:
    """Evaluates scalp microstructural setups for shadow tracking only.

    Each evaluate_* method returns a TradeSetup with the proper setup_type and
    TP/SL derived from settings.SCALP_SIGNAL_PARAMS, or None if no signal fires.

    Wiring contract:
    - Caller passes per-pair candle list (1m primary), optional MarketSnapshot.
    - Returned TradeSetup must have setup_type in settings.SCALP_SETUP_TYPES.
    - Pipeline routes to shadow_monitor when setup_type is also in SHADOW_MODE_SETUPS.
    - Per-signal time_stop_seconds applied by shadow_monitor (separate plumbing
      added in the time-stop commit).
    """

    def __init__(self) -> None:
        self._params = settings.SCALP_SIGNAL_PARAMS

    def evaluate_liq_reclaim(
        self,
        pair: str,
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot],
        now_ms: Optional[int] = None,
    ) -> Optional[TradeSetup]:
        """Signal 1 — Liquidation reclaim.

        Trigger: any OI flush event for this pair within the last 5 minutes
        (data_service.oi_flush_detector → MarketSnapshot.recent_oi_flushes).
        Confirmation: most recent confirmed candle has a wick >= 0.5% on one
        side, larger than the wick on the other side, and closes back inside
        the previous 20-bar range.
        Direction: counter to the dominant wick (lower wick → long, upper wick → short).

        Returns a TradeSetup ready for shadow tracking, or None if no signal.

        now_ms: optional injected clock for tests. Defaults to time.time() * 1000.
        """
        if not settings.SCALP_SHADOW_ENABLED:
            return None

        params = settings.SCALP_SIGNAL_PARAMS.get("scalp_liq_reclaim_v1")
        if not params:
            return None

        if not candles:
            return None
        # Need at least lookback + 1 candles. The trigger candle is candles[-1];
        # the inside-range check looks at the prior _LIQ_RECLAIM_LOOKBACK_BARS.
        if len(candles) < _LIQ_RECLAIM_LOOKBACK_BARS + 1:
            return None

        trigger = candles[-1]
        if not trigger.confirmed:
            return None
        if trigger.close <= 0:
            return None

        # Trigger gate: a fresh OI flush on this pair anchors the signal.
        if snapshot is None or not snapshot.recent_oi_flushes:
            return None
        clock_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        cutoff_ms = clock_ms - _LIQ_RECLAIM_FLUSH_MAX_AGE_MS
        fresh_flush = next(
            (f for f in snapshot.recent_oi_flushes
             if f.pair == pair and f.timestamp >= cutoff_ms),
            None,
        )
        if fresh_flush is None:
            return None

        # Wick computation. Body is the open-close span; wicks are the rest.
        body_top = max(trigger.open, trigger.close)
        body_bottom = min(trigger.open, trigger.close)
        upper_wick = max(0.0, trigger.high - body_top)
        lower_wick = max(0.0, body_bottom - trigger.low)
        upper_wick_pct = upper_wick / trigger.close
        lower_wick_pct = lower_wick / trigger.close

        if (lower_wick_pct >= _LIQ_RECLAIM_WICK_THRESHOLD
                and lower_wick_pct > upper_wick_pct):
            direction = "long"
            dominant_wick_pct = lower_wick_pct
        elif (upper_wick_pct >= _LIQ_RECLAIM_WICK_THRESHOLD
                and upper_wick_pct > lower_wick_pct):
            direction = "short"
            dominant_wick_pct = upper_wick_pct
        else:
            return None

        # Inside-range confirmation. The trigger close must sit within the
        # high/low envelope of the prior 20 candles. This excludes momentum
        # breakouts (where the close already left the range) and keeps only
        # cases where the wick was rejected back into prior structure.
        prior = candles[-(_LIQ_RECLAIM_LOOKBACK_BARS + 1):-1]
        if len(prior) != _LIQ_RECLAIM_LOOKBACK_BARS:
            return None
        prior_high = max(c.high for c in prior)
        prior_low = min(c.low for c in prior)
        if not (prior_low <= trigger.close <= prior_high):
            return None

        # Build TP/SL from configured percentages. tp1 sits at the midpoint
        # of the entry-tp2 segment, matching the existing breakeven-on-TP1
        # convention used by ShadowMonitor.
        tp_pct = params["tp_pct"] / 100.0
        sl_pct = params["sl_pct"] / 100.0
        entry = trigger.close

        if direction == "long":
            sl = entry * (1.0 - sl_pct)
            tp2 = entry * (1.0 + tp_pct)
            tp1 = entry + (tp2 - entry) * 0.5
        else:
            sl = entry * (1.0 + sl_pct)
            tp2 = entry * (1.0 - tp_pct)
            tp1 = entry - (entry - tp2) * 0.5

        confluences = [
            "oi_flush",
            f"{'lower' if direction == 'long' else 'upper'}_wick_reclaim",
            f"wick_pct={dominant_wick_pct * 100:.2f}",
            f"flush_side={fresh_flush.side}",
        ]

        logger.info(
            f"Scalp liq_reclaim: {pair} {direction} entry={entry:.4f} "
            f"sl={sl:.4f} tp2={tp2:.4f} wick={dominant_wick_pct * 100:.2f}% "
            f"flush_side={fresh_flush.side} flush_age_ms={clock_ms - fresh_flush.timestamp}"
        )

        return TradeSetup(
            timestamp=trigger.timestamp,
            pair=pair,
            direction=direction,
            setup_type="scalp_liq_reclaim_v1",
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias="scalp",
            ob_timeframe=trigger.timeframe,
        )

    def evaluate_sweep_choch(
        self,
        pair: str,
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 2 — Sweep + CHoCH (close-back-inside).

        Two-bar pattern:
        - Sweep candle (`candles[-2]`): pierces the high or low of the prior
          `_SWEEP_CHOCH_LOOKBACK_BARS` bars (those bars exclude both the sweep
          and confirm candles).
        - Confirm candle (`candles[-1]`): closes back inside the prior
          envelope and has a body >= `_SWEEP_CHOCH_MIN_BODY_RATIO` of its
          full range. The body direction must reject the sweep.

        Direction: counter to the sweep (high swept => short, low swept => long).
        TP/SL/time_stop come from settings.SCALP_SIGNAL_PARAMS.

        snapshot is currently unused by this signal but kept in the signature
        so all scalp evaluators present a uniform shape to the caller.
        """
        if not settings.SCALP_SHADOW_ENABLED:
            return None

        params = settings.SCALP_SIGNAL_PARAMS.get("scalp_sweep_choch_v1")
        if not params:
            return None

        # Need: 20 prior bars + 1 sweep + 1 confirm = 22 candles minimum.
        required = _SWEEP_CHOCH_LOOKBACK_BARS + 2
        if not candles or len(candles) < required:
            return None

        confirm = candles[-1]
        sweep = candles[-2]
        if not confirm.confirmed or not sweep.confirmed:
            return None
        if confirm.close <= 0 or sweep.close <= 0:
            return None

        prior = candles[-(required):-2]
        if len(prior) != _SWEEP_CHOCH_LOOKBACK_BARS:
            return None
        prior_high = max(c.high for c in prior)
        prior_low = min(c.low for c in prior)

        direction: Optional[str] = None
        sweep_side: Optional[str] = None
        if sweep.high > prior_high and confirm.close < prior_high:
            # High swept then reclaimed back inside — short setup.
            direction = "short"
            sweep_side = "high"
        elif sweep.low < prior_low and confirm.close > prior_low:
            # Low swept then reclaimed back inside — long setup.
            direction = "long"
            sweep_side = "low"
        else:
            return None

        # Confirm candle must close in the direction of the rejection.
        # For a short, rejection = close below open. For a long, close above open.
        if direction == "short" and confirm.close >= confirm.open:
            return None
        if direction == "long" and confirm.close <= confirm.open:
            return None

        confirm_range = confirm.high - confirm.low
        if confirm_range <= 0:
            return None
        body_ratio = abs(confirm.close - confirm.open) / confirm_range
        if body_ratio < _SWEEP_CHOCH_MIN_BODY_RATIO:
            return None

        tp_pct = params["tp_pct"] / 100.0
        sl_pct = params["sl_pct"] / 100.0
        entry = confirm.close

        if direction == "long":
            sl = entry * (1.0 - sl_pct)
            tp2 = entry * (1.0 + tp_pct)
            tp1 = entry + (tp2 - entry) * 0.5
        else:
            sl = entry * (1.0 + sl_pct)
            tp2 = entry * (1.0 - tp_pct)
            tp1 = entry - (entry - tp2) * 0.5

        confluences = [
            f"sweep_{sweep_side}",
            "choch_close_inside",
            f"body_ratio={body_ratio:.2f}",
            f"prior_high={prior_high:.4f}",
            f"prior_low={prior_low:.4f}",
        ]

        logger.info(
            f"Scalp sweep_choch: {pair} {direction} entry={entry:.4f} "
            f"sl={sl:.4f} tp2={tp2:.4f} sweep_side={sweep_side} "
            f"body_ratio={body_ratio:.2f}"
        )

        return TradeSetup(
            timestamp=confirm.timestamp,
            pair=pair,
            direction=direction,
            setup_type="scalp_sweep_choch_v1",
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias="scalp",
            ob_timeframe=confirm.timeframe,
        )

    def evaluate_vol_cvd_divergence(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 3 — Volume Z-score + CVD divergence.

        Trigger: 1m volume >= 3 sigma over 20-period mean AND CVD direction
        opposite to candle direction.
        Confirmation: orderbook spread <= 2bps.
        Direction: dirección de CVD (no del precio).
        """
        return None

    def evaluate_funding_extreme(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 4 — Funding extreme + flat price.

        Trigger: funding rate >= 0.05% (8h) AND price range last 30min <= 0.3%.
        Direction: counter to funding.
        """
        return None

    def evaluate_random_baseline(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Control — Random baseline.

        Uniform random emission, frequency-matched to combined S1-S4 firing rate.
        Direction 50/50. TP/SL/time_stop rotated to match the signal under comparison.
        Purpose: any "winning" signal must beat this baseline by >= 15pp WR.
        """
        return None
