"""
Scalp Shadow Signals — v1 detectors with v2 filter overlay (2026-05-05).

Microstructural scalping signals tested in shadow mode only. No live execution.
Plan: docs/plans/scalp_shadow_v1.md. SYSTEM_BASELINE §"Scalp Shadow v1".

Five candidates (4 signals + control baseline):
- scalp_liq_reclaim_v1     — OI drop + wick reclaim
- scalp_sweep_choch_v1     — sweep of 20-bar high/low + 1m CHoCH
                             (v2 filters: ADX(14) + book_imbalance fade gate)
- scalp_vol_cvd_div_v1     — 3-sigma volume + CVD/price divergence
- scalp_funding_extreme_v1 — funding extreme + flat price last 30min
- scalp_random_baseline_v1 — random control, frequency-matched

Per-signal TP/SL/time_stop comes from settings.SCALP_SIGNAL_PARAMS.
ShadowMonitor reads time_stop_seconds from there at add_shadow time.

Detectors run on settings.SCALP_TIMEFRAME (default 5m) until 1m candle
fetching is added in a follow-up commit.
"""

import random
import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.ml_features import _compute_adx
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

# Volume Z-score + CVD divergence:
# Lookback bars for the volume mean/std baseline (excludes trigger candle).
_VOL_CVD_LOOKBACK_BARS = 20
# Minimum trigger-volume Z-score over the baseline.
_VOL_CVD_Z_THRESHOLD = 3.0
# Minimum |cvd_5m| / (buy_volume + sell_volume) — filters out borderline
# imbalances where the sign of CVD is just noise. 0.20 ~ a 60/40 split.
_VOL_CVD_MIN_IMBALANCE = 0.20
# Max acceptable orderbook spread as fraction of mid. 2 bps = 0.0002.
_VOL_CVD_MAX_SPREAD = 0.0002

# Funding extreme + flat price:
# Min |funding_rate| (8h convention, fraction not pct) to qualify as extreme.
# 0.0005 = 0.05% per 8h period (~0.15%/day annualized ~55%).
_FUNDING_RATE_THRESHOLD = 0.0005
# Number of bars to scan for the flat-price condition. Tuned for SCALP_TIMEFRAME=5m
# (6 * 5min = 30min). Bump when 1m fetcher lands.
_FUNDING_FLAT_LOOKBACK_BARS = 6
# Max price range over the lookback window as fraction of close. 0.003 = 0.3%.
_FUNDING_FLAT_RANGE_THRESHOLD = 0.003


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
        orderbook: Optional[dict] = None,
    ) -> Optional[TradeSetup]:
        """Signal 2 — Sweep + CHoCH (close-back-inside).

        Two-bar pattern:
        - Sweep candle (`candles[-2]`): pierces the high or low of the prior
          `_SWEEP_CHOCH_LOOKBACK_BARS` bars (those bars exclude both the sweep
          and confirm candles).
        - Confirm candle (`candles[-1]`): closes back inside the prior
          envelope and has a body >= `_SWEEP_CHOCH_MIN_BODY_RATIO` of its
          full range. The body direction must reject the sweep.

        v2 fade-pattern filters (added 2026-05-05 after 76-outcome v1 review):
        - ADX(14) on scalp timeframe must be >= SCALP_SWEEP_CHOCH_MIN_ADX.
          Sub-trend regimes (range/compression/hostile) dominated v1 SLs.
        - Orderbook imbalance gate when orderbook is available:
            long  → book_imbalance < SCALP_SWEEP_CHOCH_BOOK_IMB_LONG_MAX
            short → book_imbalance > SCALP_SWEEP_CHOCH_BOOK_IMB_SHORT_MIN
          v1 data: long SL avg imb 16.0 vs long TP avg 1.2; short TP avg 11.6
          vs short SL avg 4.5. Stacked bids that get swept = institutional
          absorption, not real support — the sweep is the absorption signature.
        Orderbook missing → skip imbalance gate (avoid blocking on stale data).

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

        # v2 filter — ADX trend gate. Reject sub-trend regimes where the
        # 0.15% SL gets stopped on noise before the thesis develops.
        adx_result = _compute_adx(candles, period=14)
        if adx_result is None:
            # Insufficient candles for ADX warmup — block emission rather
            # than emit blind in a setup with a known noise problem.
            logger.debug(
                f"sweep_choch {pair}: ADX unavailable ({len(candles)} candles), skip"
            )
            return None
        adx_value = adx_result[0]
        if adx_value < settings.SCALP_SWEEP_CHOCH_MIN_ADX:
            logger.debug(
                f"sweep_choch {pair} {direction}: ADX {adx_value:.1f} < "
                f"{settings.SCALP_SWEEP_CHOCH_MIN_ADX} — sub-trend regime, skip"
            )
            return None

        # v2 filter — orderbook imbalance fade gate. When orderbook is
        # missing fall through (do not block on stale data).
        if orderbook is not None:
            depth_bid = orderbook.get("depth_bid_usd") or 0.0
            depth_ask = orderbook.get("depth_ask_usd") or 0.0
            if depth_bid > 0 and depth_ask > 0:
                imb = depth_bid / depth_ask
                long_max = settings.SCALP_SWEEP_CHOCH_BOOK_IMB_LONG_MAX
                short_min = settings.SCALP_SWEEP_CHOCH_BOOK_IMB_SHORT_MIN
                if direction == "long" and imb >= long_max:
                    logger.debug(
                        f"sweep_choch {pair} long: book_imb {imb:.2f} >= "
                        f"{long_max} — bid stacking, fade pattern, skip"
                    )
                    return None
                if direction == "short" and imb <= short_min:
                    logger.debug(
                        f"sweep_choch {pair} short: book_imb {imb:.2f} <= "
                        f"{short_min} — no bid stack to fade, skip"
                    )
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
            f"adx_14={adx_value:.1f}",
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
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot],
        orderbook: Optional[dict] = None,
    ) -> Optional[TradeSetup]:
        """Signal 3 — Volume Z-score + CVD divergence.

        Trigger: trigger candle volume >= `_VOL_CVD_Z_THRESHOLD` standard
        deviations above the mean of the prior `_VOL_CVD_LOOKBACK_BARS` bars
        AND the candle's price direction is opposite to the CVD direction
        (price up while flow is selling, or price down while flow is buying).

        Confirmation: orderbook spread <= `_VOL_CVD_MAX_SPREAD` (no chaos).

        Direction: side of the CVD imbalance. CVD positive (buyers dominant)
        => long; CVD negative => short. The price-vs-flow divergence is what
        gives this its mean-reversion thesis.
        """
        if not settings.SCALP_SHADOW_ENABLED:
            return None

        params = settings.SCALP_SIGNAL_PARAMS.get("scalp_vol_cvd_div_v1")
        if not params:
            return None

        if not candles or len(candles) < _VOL_CVD_LOOKBACK_BARS + 1:
            return None

        trigger = candles[-1]
        if not trigger.confirmed or trigger.close <= 0:
            return None

        # Need CVD flow data and a clean book.
        if snapshot is None or snapshot.cvd is None:
            return None
        if orderbook is None:
            return None
        spread = orderbook.get("spread", 0.0) or 0.0
        if spread <= 0 or spread > _VOL_CVD_MAX_SPREAD:
            return None

        prior = candles[-(_VOL_CVD_LOOKBACK_BARS + 1):-1]
        if len(prior) != _VOL_CVD_LOOKBACK_BARS:
            return None
        volumes = [c.volume for c in prior]
        mean_vol = sum(volumes) / len(volumes)
        var_vol = sum((v - mean_vol) ** 2 for v in volumes) / len(volumes)
        std_vol = var_vol ** 0.5
        if std_vol <= 0:
            return None
        z_score = (trigger.volume - mean_vol) / std_vol
        if z_score < _VOL_CVD_Z_THRESHOLD:
            return None

        cvd_5m = snapshot.cvd.cvd_5m
        total_flow = snapshot.cvd.buy_volume + snapshot.cvd.sell_volume
        if total_flow <= 0:
            return None
        cvd_imbalance = abs(cvd_5m) / total_flow
        if cvd_imbalance < _VOL_CVD_MIN_IMBALANCE:
            return None

        # Candle direction must be opposite to CVD direction (divergence).
        if trigger.close > trigger.open:
            candle_dir = "bull"
        elif trigger.close < trigger.open:
            candle_dir = "bear"
        else:
            return None  # doji — no clear price direction
        if cvd_5m > 0:
            cvd_dir = "bull"
        elif cvd_5m < 0:
            cvd_dir = "bear"
        else:
            return None
        if candle_dir == cvd_dir:
            return None  # aligned — no divergence

        direction = "long" if cvd_dir == "bull" else "short"

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
            f"vol_z={z_score:.2f}",
            f"cvd_imbalance={cvd_imbalance:.2f}",
            f"candle_dir={candle_dir}",
            f"cvd_dir={cvd_dir}",
            f"spread_bps={spread * 1e4:.2f}",
        ]

        logger.info(
            f"Scalp vol_cvd_div: {pair} {direction} entry={entry:.4f} "
            f"sl={sl:.4f} tp2={tp2:.4f} z={z_score:.2f} "
            f"cvd_imb={cvd_imbalance:.2f} spread_bps={spread * 1e4:.2f}"
        )

        return TradeSetup(
            timestamp=trigger.timestamp,
            pair=pair,
            direction=direction,
            setup_type="scalp_vol_cvd_div_v1",
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias="scalp",
            ob_timeframe=trigger.timeframe,
        )

    def evaluate_funding_extreme(
        self,
        pair: str,
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 4 — Funding extreme + flat price.

        Trigger: |funding_rate| >= `_FUNDING_RATE_THRESHOLD` (8h convention)
        AND the price range over the last `_FUNDING_FLAT_LOOKBACK_BARS`
        candles is <= `_FUNDING_FLAT_RANGE_THRESHOLD` of the trigger close.

        Edge thesis: a crowded trade (extreme funding) sitting on a tight
        range is primed to flush against the crowd. Counter-funding entry:
        positive funding (longs pay) => short, negative funding => long.

        TP/SL/time_stop come from settings.SCALP_SIGNAL_PARAMS.
        """
        if not settings.SCALP_SHADOW_ENABLED:
            return None

        params = settings.SCALP_SIGNAL_PARAMS.get("scalp_funding_extreme_v1")
        if not params:
            return None

        if not candles or len(candles) < _FUNDING_FLAT_LOOKBACK_BARS:
            return None

        trigger = candles[-1]
        if not trigger.confirmed or trigger.close <= 0:
            return None

        if snapshot is None or snapshot.funding is None:
            return None
        funding_rate = snapshot.funding.rate
        if abs(funding_rate) < _FUNDING_RATE_THRESHOLD:
            return None

        window = candles[-_FUNDING_FLAT_LOOKBACK_BARS:]
        if len(window) != _FUNDING_FLAT_LOOKBACK_BARS:
            return None
        window_high = max(c.high for c in window)
        window_low = min(c.low for c in window)
        range_pct = (window_high - window_low) / trigger.close
        if range_pct > _FUNDING_FLAT_RANGE_THRESHOLD:
            return None

        # Counter-funding direction. Positive funding = longs paying = crowded
        # long => short setup. Negative funding = shorts paying = crowded short
        # => long setup.
        direction = "short" if funding_rate > 0 else "long"

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
            f"funding_rate={funding_rate * 100:.4f}",
            f"range_pct={range_pct * 100:.3f}",
            f"flat_bars={_FUNDING_FLAT_LOOKBACK_BARS}",
        ]

        logger.info(
            f"Scalp funding_extreme: {pair} {direction} entry={entry:.4f} "
            f"sl={sl:.4f} tp2={tp2:.4f} funding={funding_rate * 100:.4f}% "
            f"range={range_pct * 100:.3f}%"
        )

        return TradeSetup(
            timestamp=trigger.timestamp,
            pair=pair,
            direction=direction,
            setup_type="scalp_funding_extreme_v1",
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias="scalp",
            ob_timeframe=trigger.timeframe,
        )

    def evaluate_random_baseline(
        self,
        pair: str,
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot],
        rng: Optional[random.Random] = None,
    ) -> Optional[TradeSetup]:
        """Control — Random baseline.

        Fires with probability `settings.SCALP_BASELINE_FIRE_PROB` per call
        (per pair, per evaluated candle). Direction is 50/50 uniform.
        TP/SL/time_stop come from settings.SCALP_SIGNAL_PARAMS for
        scalp_random_baseline_v1 (defaults match Signal 1 so the comparison
        is apples to apples).

        rng: optional injected random.Random for deterministic tests.
        """
        if not settings.SCALP_SHADOW_ENABLED:
            return None

        params = settings.SCALP_SIGNAL_PARAMS.get("scalp_random_baseline_v1")
        if not params:
            return None

        if not candles:
            return None
        trigger = candles[-1]
        if not trigger.confirmed or trigger.close <= 0:
            return None

        r = rng if rng is not None else random
        if r.random() >= settings.SCALP_BASELINE_FIRE_PROB:
            return None

        direction = "long" if r.random() < 0.5 else "short"

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

        confluences = ["random_baseline"]

        logger.info(
            f"Scalp random_baseline: {pair} {direction} entry={entry:.4f} "
            f"sl={sl:.4f} tp2={tp2:.4f}"
        )

        return TradeSetup(
            timestamp=trigger.timestamp,
            pair=pair,
            direction=direction,
            setup_type="scalp_random_baseline_v1",
            entry_price=entry,
            sl_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias="scalp",
            ob_timeframe=trigger.timeframe,
        )
