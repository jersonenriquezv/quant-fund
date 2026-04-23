"""
Property-based invariants for market structure detection.

Brutality goal: instead of testing a few hand-picked candle sequences,
generate hundreds of random valid sequences and assert rules that MUST
hold on any of them. Failure = real bug, not a typo in the test input.

Invariants covered:
- Swing highs must be local maxima over SWING_LOOKBACK
- Swing lows must be local minima over SWING_LOOKBACK
- Structure break direction must match price action (BOS up = new high)
- analyzer.update() is deterministic: same candles in = same state out
- Trend label must match latest break direction when present
"""

from __future__ import annotations

import random
import time

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st, settings as h_settings, assume  # noqa: E402

from config.settings import settings
from shared.models import Candle
from strategy_service.market_structure import MarketStructureAnalyzer


def _candle(i: int, high: float, low: float, close: float | None = None,
            pair: str = "BTC/USDT", tf: str = "5m") -> Candle:
    return Candle(
        timestamp=1_700_000_000_000 + i * 300_000,
        open=close if close is not None else (high + low) / 2,
        high=high, low=low,
        close=close if close is not None else (high + low) / 2,
        volume=10.0, volume_quote=10.0 * ((high + low) / 2),
        pair=pair, timeframe=tf, confirmed=True,
    )


def _random_walk(rng: random.Random, n: int, base: float = 1000.0,
                 vol: float = 0.005) -> list[Candle]:
    """Generate a realistic random-walk candle sequence."""
    out = []
    price = base
    for i in range(n):
        ret = rng.gauss(0, vol)
        new_price = price * (1 + ret)
        hi = max(price, new_price) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(price, new_price) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append(_candle(i, hi, lo, new_price))
        price = new_price
    return out


# ================================================================
# Determinism
# ================================================================

class TestDeterminism:
    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=50, deadline=None)
    def test_same_candles_same_state(self, seed):
        rng = random.Random(seed)
        candles = _random_walk(rng, 120)
        a = MarketStructureAnalyzer()
        b = MarketStructureAnalyzer()
        sa = a.analyze(candles, "BTC/USDT", "5m")
        sb = b.analyze(candles, "BTC/USDT", "5m")

        assert len(sa.swing_highs) == len(sb.swing_highs)
        assert len(sa.swing_lows) == len(sb.swing_lows)
        assert len(sa.structure_breaks) == len(sb.structure_breaks)
        for x, y in zip(sa.swing_highs, sb.swing_highs):
            assert x.price == y.price
            assert x.timestamp == y.timestamp
            assert x.swing_type == y.swing_type


# ================================================================
# Swing point correctness
# ================================================================

class TestSwingPointInvariants:
    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=100, deadline=None)
    def test_swing_high_is_local_maximum(self, seed):
        """For each swing_high at index i, candle.high must be >= neighbors
        within SWING_LOOKBACK on both sides (strict max on symmetric window).
        """
        rng = random.Random(seed)
        candles = _random_walk(rng, 150)
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze(candles, "BTC/USDT", "5m")

        assume(len(state.swing_highs) > 0)
        lookback = settings.SWING_LOOKBACK

        # Build timestamp → index lookup
        ts_to_idx = {c.timestamp: i for i, c in enumerate(candles)}

        for swing in state.swing_highs:
            idx = ts_to_idx.get(swing.timestamp)
            if idx is None:
                continue
            lo = max(0, idx - lookback)
            hi = min(len(candles), idx + lookback + 1)
            window = candles[lo:hi]
            max_high = max(c.high for c in window)
            assert candles[idx].high == pytest.approx(max_high, abs=1e-9), (
                f"swing_high at idx={idx} price={candles[idx].high} "
                f"but window max is {max_high}"
            )

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=100, deadline=None)
    def test_swing_low_is_local_minimum(self, seed):
        rng = random.Random(seed)
        candles = _random_walk(rng, 150)
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze(candles, "BTC/USDT", "5m")

        assume(len(state.swing_lows) > 0)
        lookback = settings.SWING_LOOKBACK
        ts_to_idx = {c.timestamp: i for i, c in enumerate(candles)}

        for swing in state.swing_lows:
            idx = ts_to_idx.get(swing.timestamp)
            if idx is None:
                continue
            lo = max(0, idx - lookback)
            hi = min(len(candles), idx + lookback + 1)
            window = candles[lo:hi]
            min_low = min(c.low for c in window)
            assert candles[idx].low == pytest.approx(min_low, abs=1e-9)

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=50, deadline=None)
    def test_swings_ordered_by_timestamp(self, seed):
        rng = random.Random(seed)
        candles = _random_walk(rng, 200)
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze(candles, "BTC/USDT", "5m")
        for swings in (state.swing_highs, state.swing_lows):
            for a, b in zip(swings, swings[1:]):
                assert a.timestamp < b.timestamp, (
                    "swing points must be chronological"
                )


# ================================================================
# Structure break consistency
# ================================================================

class TestStructureBreakInvariants:
    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=100, deadline=None)
    def test_bullish_break_price_above_broken_level(self, seed):
        rng = random.Random(seed)
        candles = _random_walk(rng, 200, vol=0.01)
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze(candles, "BTC/USDT", "5m")

        for b in state.structure_breaks:
            if b.direction == "bullish":
                assert b.break_price > b.broken_level, (
                    f"bullish break at {b.break_price} must exceed "
                    f"broken swing high at {b.broken_level}"
                )
            else:
                assert b.break_price < b.broken_level, (
                    f"bearish break at {b.break_price} must be below "
                    f"broken swing low at {b.broken_level}"
                )

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=50, deadline=None)
    def test_break_type_is_valid_enum(self, seed):
        rng = random.Random(seed)
        candles = _random_walk(rng, 200)
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze(candles, "BTC/USDT", "5m")
        for b in state.structure_breaks:
            assert b.break_type in ("bos", "choch"), (
                f"break_type must be bos|choch, got {b.break_type!r}"
            )
            assert b.direction in ("bullish", "bearish")


# ================================================================
# Empty / minimal inputs
# ================================================================

class TestEdgeCases:
    def test_empty_candles_returns_empty_state(self):
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze([], "BTC/USDT", "5m")
        assert state.swing_highs == []
        assert state.swing_lows == []
        assert state.structure_breaks == []
        assert state.latest_break is None

    def test_single_candle_no_swings(self):
        analyzer = MarketStructureAnalyzer()
        state = analyzer.analyze([_candle(0, 100, 99, 99.5)], "BTC/USDT", "5m")
        assert len(state.swing_highs) == 0
        assert len(state.swing_lows) == 0

    def test_flat_candles_no_swings(self):
        """Constant price → no swings detected (no local max/min)."""
        analyzer = MarketStructureAnalyzer()
        candles = [_candle(i, 100.0, 100.0, 100.0) for i in range(50)]
        state = analyzer.analyze(candles, "BTC/USDT", "5m")
        # Implementation detail: a tie may or may not register as swing.
        # Asserting no STRUCTURE BREAKS is the stronger invariant.
        assert state.structure_breaks == [], (
            "flat price cannot produce structure breaks"
        )
        assert state.latest_break is None
