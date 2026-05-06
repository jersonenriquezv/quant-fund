"""Engine 1 — Trend-Pullback / Impulse Retest tests.

Coverage:
- compute_atr: insufficient data, basic correctness.
- _qualify_impulse: directional purity, body ratio, ATR threshold,
  malformed inputs.
- _qualify_pullback: retrace bounds, opposing-body veto, malformed.
- detect_impulse_pullback: returns most recent valid pair.
- compute_entry / compute_sl / compute_tp: geometry sanity.
- TrendPullbackEngine.evaluate:
  - returns None when HTF undefined / insufficient candles / no pattern.
  - emits TradeSetup with engine1_trend_pullback type when valid.
  - direction must align with HTF bias.
  - entry distance gate enforced.
  - net R:R gate enforced (rejects when target is too close).
  - confluences contain engine-specific tags.
"""

from typing import Optional

import pytest

from shared.models import Candle, TradeSetup
from strategy_service.engines.trend_pullback import (
    SETUP_TYPE,
    ENTRY_FIB_TARGET,
    ENTRY_MAX_ATR_MULT,
    ImpulseLeg,
    Pullback,
    TrendPullbackEngine,
    _qualify_impulse,
    _qualify_pullback,
    compute_atr,
    compute_entry,
    compute_sl,
    compute_tp,
    detect_impulse_pullback,
    is_entry_within_atr,
)


def _candle(
    open_: float, high: float, low: float, close: float,
    timestamp: int = 0,
    pair: str = "BTC/USDT",
    timeframe: str = "15m",
    volume: float = 10.0,
) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=open_, high=high, low=low, close=close,
        volume=volume, volume_quote=volume * close,
        pair=pair, timeframe=timeframe, confirmed=True,
    )


def _bullish_impulse(start: float = 100.0, step: float = 1.0, count: int = 5):
    """Build N candles each moving by `step` (signed; negative = bearish)."""
    candles = []
    abs_step = abs(step)
    for i in range(count):
        o = start + i * step
        c = o + step * 0.9  # body 90% of range, in step direction
        h = max(o, c) + abs_step * 0.05
        l = min(o, c) - abs_step * 0.05
        candles.append(_candle(o, h, l, c, timestamp=i * 1000))
    return candles


def _bearish_pullback(start: float, step: float = 0.3, count: int = 3):
    """Build N bearish pullback candles each falling by `step`."""
    candles = []
    for i in range(count):
        o = start - i * step
        c = o - step * 0.7  # body 70% of range
        h = o + step * 0.15
        l = c - step * 0.15
        candles.append(_candle(o, h, l, c, timestamp=(100 + i) * 1000))
    return candles


# ============================================================
# compute_atr
# ============================================================

class TestComputeATR:
    def test_returns_none_on_insufficient_data(self):
        candles = [_candle(100, 101, 99, 100.5)] * 10
        assert compute_atr(candles, period=14) is None

    def test_returns_none_on_empty(self):
        assert compute_atr([], period=14) is None

    def test_basic_atr(self):
        # 20 candles with high-low = 2 each. Closes flat, no gaps.
        candles = [
            _candle(100, 101, 99, 100, timestamp=i * 1000) for i in range(20)
        ]
        atr = compute_atr(candles, period=14)
        assert atr is not None
        assert abs(atr - 2.0) < 1e-6


# ============================================================
# _qualify_impulse
# ============================================================

class TestQualifyImpulse:
    def test_qualifies_bullish_impulse(self):
        candles = _bullish_impulse(start=100.0, step=2.0, count=5)
        atr = 0.5
        impulse = _qualify_impulse(candles, start_idx=0, atr=atr)
        assert impulse is not None
        assert impulse.direction == "long"
        assert impulse.candle_count == 5
        assert impulse.atr_multiple > 2.0

    def test_qualifies_bearish_impulse(self):
        candles = _bullish_impulse(start=100.0, step=-2.0, count=5)
        atr = 0.5
        impulse = _qualify_impulse(candles, start_idx=0, atr=atr)
        assert impulse is not None
        assert impulse.direction == "short"

    def test_rejects_too_few_candles(self):
        candles = _bullish_impulse(count=2)
        assert _qualify_impulse(candles, 0, atr=0.5) is None

    def test_rejects_low_atr_multiple(self):
        # Tiny step: 5 candles × 0.05 = 0.25 displacement at ATR=2.0 → 0.125x
        candles = _bullish_impulse(start=100.0, step=0.05, count=5)
        impulse = _qualify_impulse(candles, 0, atr=2.0)
        assert impulse is None

    def test_rejects_low_directional_purity(self):
        # 4 bullish + 4 bearish alternating → directional ~50%
        candles = []
        price = 100.0
        for i in range(8):
            sign = 1 if i % 2 == 0 else -1
            o = price
            c = o + sign * 1.0
            h = max(o, c) + 0.1
            l = min(o, c) - 0.1
            candles.append(_candle(o, h, l, c, timestamp=i * 1000))
            price = c
        impulse = _qualify_impulse(candles, 0, atr=0.5)
        assert impulse is None

    def test_rejects_low_body_ratio(self):
        # Candles with huge wicks but tiny bodies — pure noise.
        candles = []
        price = 100.0
        for i in range(5):
            o = price
            c = o + 0.05  # 0.05 body
            h = c + 5.0   # huge upper wick
            l = o - 5.0   # huge lower wick
            candles.append(_candle(o, h, l, c, timestamp=i * 1000))
            price = c
        impulse = _qualify_impulse(candles, 0, atr=0.5)
        assert impulse is None

    def test_rejects_zero_atr(self):
        candles = _bullish_impulse(count=5)
        assert _qualify_impulse(candles, 0, atr=0.0) is None
        assert _qualify_impulse(candles, 0, atr=-1.0) is None

    def test_origin_is_first_candle_low_for_long(self):
        candles = _bullish_impulse(start=100.0, step=2.0, count=5)
        impulse = _qualify_impulse(candles, 0, atr=0.5)
        assert impulse is not None
        # Origin is first candle's low (open ± noise)
        assert impulse.origin_price == candles[0].low

    def test_peak_is_last_candle_high_for_long(self):
        candles = _bullish_impulse(start=100.0, step=2.0, count=5)
        impulse = _qualify_impulse(candles, 0, atr=0.5)
        assert impulse is not None
        assert impulse.peak_price == candles[-1].high


# ============================================================
# _qualify_pullback
# ============================================================

class TestQualifyPullback:
    def _impulse_long(self):
        # Use real candles to derive the structure ratio sanely.
        candles = _bullish_impulse(start=100.0, step=2.0, count=5)
        return _qualify_impulse(candles, 0, atr=0.5)

    def test_qualifies_clean_pullback(self):
        impulse = self._impulse_long()
        assert impulse is not None
        # peak ~110.05 (from helper). Pullback to ~108 = ~21% retrace.
        peak = impulse.peak_price
        impulse_dist = peak - impulse.origin_price
        target = peak - impulse_dist * 0.45  # ~45% retrace
        candles = []
        price = peak
        step = (peak - target) / 3
        for i in range(3):
            o = price
            c = o - step * 0.7
            h = o + step * 0.05
            l = c - step * 0.05
            candles.append(_candle(o, h, l, c, timestamp=(100 + i) * 1000))
            price = c
        pullback = _qualify_pullback(candles, 100, impulse, atr=0.5)
        assert pullback is not None
        assert 0.30 <= pullback.depth_pct <= 0.85

    def test_rejects_too_shallow_retrace(self):
        impulse = self._impulse_long()
        assert impulse is not None
        # Retrace only 5%
        peak = impulse.peak_price
        candles = []
        price = peak
        for i in range(3):
            o = price
            c = o - 0.05
            h = o + 0.02
            l = c - 0.02
            candles.append(_candle(o, h, l, c, timestamp=(100 + i) * 1000))
            price = c
        assert _qualify_pullback(candles, 100, impulse, atr=0.5) is None

    def test_rejects_retrace_past_origin(self):
        impulse = self._impulse_long()
        assert impulse is not None
        # Pullback collapses below the origin entirely
        below = impulse.origin_price - 5.0
        candles = []
        price = impulse.peak_price
        step = (impulse.peak_price - below) / 3
        for i in range(3):
            o = price
            c = o - step
            h = o + 0.1
            l = c - 0.1
            candles.append(_candle(o, h, l, c, timestamp=(100 + i) * 1000))
            price = c
        assert _qualify_pullback(candles, 100, impulse, atr=0.5) is None

    def test_rejects_dominant_opposing_body(self):
        impulse = self._impulse_long()
        assert impulse is not None
        # Pullback with one big BULLISH (in-impulse-direction) candle in the
        # middle. Even if depth ends up valid, opposing body should veto.
        peak = impulse.peak_price
        candles = [
            _candle(peak, peak + 0.05, peak - 0.5, peak - 0.4, timestamp=100_000),
            # Big bullish candle inside the pullback
            _candle(peak - 0.4, peak + 1.0, peak - 0.5, peak + 0.8, timestamp=101_000),
            _candle(peak + 0.8, peak + 0.9, peak - 1.0, peak - 0.9, timestamp=102_000),
        ]
        # Depth check passes only if we end below peak; build last close low enough.
        result = _qualify_pullback(candles, 100, impulse, atr=0.5)
        # Either rejected by opposing body or by retrace bounds — never qualified.
        assert result is None

    def test_rejects_too_few_candles(self):
        impulse = self._impulse_long()
        peak = impulse.peak_price
        candles = [_candle(peak, peak + 0.05, peak - 0.3, peak - 0.2)]
        assert _qualify_pullback(candles, 100, impulse, atr=0.5) is None


# ============================================================
# detect_impulse_pullback
# ============================================================

class TestDetectImpulsePullback:
    def test_returns_none_on_short_history(self):
        candles = _bullish_impulse(count=2)
        assert detect_impulse_pullback(candles, atr=0.5) is None

    def test_returns_none_when_no_pullback(self):
        # Pure ramp, no retrace.
        candles = _bullish_impulse(start=100.0, step=2.0, count=10)
        result = detect_impulse_pullback(candles, atr=0.5)
        assert result is None

    def test_finds_impulse_then_pullback(self):
        # 5 bullish + 3 bearish pullback
        impulse = _bullish_impulse(start=100.0, step=2.0, count=5)
        peak = impulse[-1].close
        pullback = _bearish_pullback(start=peak, step=0.6, count=3)
        candles = impulse + pullback
        # Pad some history before the impulse
        history = [
            _candle(99.0, 100.0, 98.5, 99.5, timestamp=-(i + 1) * 1000)
            for i in range(5)
        ]
        history.reverse()
        full = history + candles
        # Reset timestamps strictly increasing
        for i, c in enumerate(full):
            full[i] = _candle(c.open, c.high, c.low, c.close, timestamp=i * 1000)

        result = detect_impulse_pullback(full, atr=0.5)
        assert result is not None
        imp, pull = result
        assert imp.direction == "long"
        assert imp.candle_count >= 3
        assert pull.candle_count >= 2


# ============================================================
# compute_entry / compute_sl / compute_tp / is_entry_within_atr
# ============================================================

class TestGeometry:
    def _impulse(self, direction="long"):
        return ImpulseLeg(
            start_idx=0,
            end_idx=4,
            direction=direction,
            origin_price=100.0,
            peak_price=110.0 if direction == "long" else 90.0,
            displacement_pct=0.10,
            atr_multiple=10.0,
            avg_body_ratio=0.7,
            candle_count=5,
        )

    def test_compute_entry_long_at_50_pct_retrace(self):
        imp = self._impulse("long")
        entry = compute_entry(imp)
        # peak=110, origin=100, 50% retrace → 105
        assert abs(entry - 105.0) < 1e-6

    def test_compute_entry_short_at_50_pct_retrace(self):
        imp = self._impulse("short")
        entry = compute_entry(imp)
        # peak=90, origin=100, 50% retrace → 95
        assert abs(entry - 95.0) < 1e-6

    def test_compute_sl_long_below_origin(self):
        imp = self._impulse("long")
        entry = 105.0
        sl = compute_sl(imp, entry, atr=1.0)
        assert sl < imp.origin_price
        # 5% buffer × 10 distance = 0.5 → SL ≤ 99.5
        assert sl <= 99.5

    def test_compute_sl_short_above_origin(self):
        imp = self._impulse("short")
        entry = 95.0
        sl = compute_sl(imp, entry, atr=1.0)
        assert sl > imp.origin_price
        assert sl >= 100.5

    def test_compute_sl_atr_floor_widens_when_too_tight(self):
        # Tiny impulse so structural SL is tighter than 1×ATR floor.
        imp = ImpulseLeg(
            start_idx=0, end_idx=4, direction="long",
            origin_price=100.0, peak_price=100.5,
            displacement_pct=0.005, atr_multiple=2.5,
            avg_body_ratio=0.7, candle_count=5,
        )
        entry = 100.25
        sl = compute_sl(imp, entry, atr=1.0)
        # 1×ATR floor → entry - 1.0 = 99.25, vs structural 100.0 - 0.025 = 99.975
        # Floor wins: SL = 99.25
        assert abs(sl - 99.25) < 1e-6

    def test_compute_tp_long_returns_tp1_at_1r_tp2_at_2r_when_no_swings(self):
        entry = 100.0
        sl = 99.0
        tp = compute_tp(entry, sl, "long", swings_htf=[])
        assert tp is not None
        tp1, tp2 = tp
        assert abs(tp1 - 101.0) < 1e-6
        assert abs(tp2 - 102.0) < 1e-6

    def test_compute_tp_rejects_when_net_rr_below_floor(self):
        # If TP2 yields < 1.6 net R:R, return None.
        entry = 100.0
        sl = 99.0
        # Force a structural swing right above TP1: 101.5 → gross 1.5, net 1.3 → reject
        tp = compute_tp(entry, sl, "long", swings_htf=[101.5])
        assert tp is None

    def test_is_entry_within_atr_pass_and_fail(self):
        assert is_entry_within_atr(entry=105.0, current_price=104.0, atr=1.0) is True
        assert is_entry_within_atr(entry=105.0, current_price=100.0, atr=1.0) is False

    def test_is_entry_within_atr_rejects_zero_inputs(self):
        assert is_entry_within_atr(entry=105.0, current_price=104.0, atr=0.0) is False
        assert is_entry_within_atr(entry=105.0, current_price=0.0, atr=1.0) is False


# ============================================================
# TrendPullbackEngine.evaluate
# ============================================================

class TestEngineEvaluate:
    def _build_full_history(self, n_history: int = 20):
        """20 flat history candles + 5 bullish impulse + 3 bearish pullback."""
        history = [
            _candle(99.5, 100.0, 99.0, 99.5, timestamp=i * 1000, volume=10.0)
            for i in range(n_history)
        ]
        impulse = _bullish_impulse(start=99.5, step=2.0, count=5)
        peak = impulse[-1].close
        pullback = _bearish_pullback(start=peak, step=0.6, count=3)

        full = history + impulse + pullback
        # Strictly increasing timestamps.
        normalized = [
            _candle(c.open, c.high, c.low, c.close, timestamp=i * 1000)
            for i, c in enumerate(full)
        ]
        return normalized

    def test_returns_none_when_htf_undefined(self):
        eng = TrendPullbackEngine()
        candles = self._build_full_history()
        out = eng.evaluate(
            pair="BTC/USDT",
            candles=candles,
            current_price=candles[-1].close,
            htf_bias="undefined",
            swings_htf=[],
        )
        assert out is None

    def test_returns_none_on_empty_candles(self):
        eng = TrendPullbackEngine()
        out = eng.evaluate(
            pair="BTC/USDT",
            candles=[],
            current_price=100.0,
            htf_bias="bullish",
            swings_htf=[],
        )
        assert out is None

    def test_returns_none_on_insufficient_history_for_atr(self):
        eng = TrendPullbackEngine()
        out = eng.evaluate(
            pair="BTC/USDT",
            candles=[_candle(100, 101, 99, 100, timestamp=i * 1000) for i in range(10)],
            current_price=100.0,
            htf_bias="bullish",
            swings_htf=[],
        )
        assert out is None

    def test_returns_none_when_direction_misaligned_with_htf(self):
        eng = TrendPullbackEngine()
        candles = self._build_full_history()
        # Impulse is bullish; HTF is bearish → mismatch.
        out = eng.evaluate(
            pair="BTC/USDT",
            candles=candles,
            current_price=candles[-1].close,
            htf_bias="bearish",
            swings_htf=[],
        )
        assert out is None

    def test_emits_setup_when_pattern_valid(self):
        eng = TrendPullbackEngine()
        candles = self._build_full_history()
        out = eng.evaluate(
            pair="BTC/USDT",
            candles=candles,
            current_price=candles[-1].close,
            htf_bias="bullish",
            swings_htf=[120.0, 130.0],  # plenty of room above
        )
        assert out is not None
        assert isinstance(out, TradeSetup)
        assert out.setup_type == SETUP_TYPE
        assert out.direction == "long"
        assert out.entry_price < out.tp1_price < out.tp2_price
        assert out.sl_price < out.entry_price
        assert "engine1_impulse_atr_" in " ".join(out.confluences)
        assert "engine1_pullback_depth_" in " ".join(out.confluences)

    def test_emits_impulse_origin_ts_in_extra_features(self):
        """Cluster dedup at the StrategyService level keys off this field.

        It must equal the timestamp of the impulse's first candle so two
        emissions over the same impulse share the same origin timestamp.
        """
        from strategy_service.engines.trend_pullback import detect_impulse_pullback, compute_atr
        eng = TrendPullbackEngine()
        candles = self._build_full_history()
        out = eng.evaluate(
            pair="BTC/USDT",
            candles=candles,
            current_price=candles[-1].close,
            htf_bias="bullish",
            swings_htf=[120.0, 130.0],
        )
        assert out is not None
        atr = compute_atr(candles, period=14)
        impulse, _ = detect_impulse_pullback(candles, atr)
        assert out.extra_features.get("engine1_impulse_origin_ts") == int(
            candles[impulse.start_idx].timestamp
        )

    def test_entry_distance_gate_rejects_far_entry(self):
        eng = TrendPullbackEngine()
        candles = self._build_full_history()
        # Pull current_price far away from entry zone (current_price implies
        # market moved 10× ATR away from where entry would land).
        out = eng.evaluate(
            pair="BTC/USDT",
            candles=candles,
            current_price=candles[-1].close + 1000.0,
            htf_bias="bullish",
            swings_htf=[120.0, 130.0],
        )
        assert out is None

    def test_target_space_gate_rejects_when_no_room(self):
        eng = TrendPullbackEngine()
        candles = self._build_full_history()
        # Place a swing high right above TP1 so net R:R falls below 1.6.
        # Find the entry first to compute a value that triggers the gate.
        out_with_room = eng.evaluate(
            pair="BTC/USDT",
            candles=candles,
            current_price=candles[-1].close,
            htf_bias="bullish",
            swings_htf=[1000.0],
        )
        assert out_with_room is not None
        risk = abs(out_with_room.entry_price - out_with_room.sl_price)
        # A swing right at TP1 + tiny offset (< MIN_RR_NET × risk) → reject.
        crowding_swing = out_with_room.entry_price + risk * 1.4
        out_crowded = eng.evaluate(
            pair="BTC/USDT",
            candles=candles,
            current_price=candles[-1].close,
            htf_bias="bullish",
            swings_htf=[crowding_swing],
        )
        assert out_crowded is None
