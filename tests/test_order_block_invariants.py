"""
Property-based invariants for Order Block detection.

Covers:
- body_low <= body_high, low <= high, and body bounds inside wick bounds
- entry_price is exactly the body midpoint
- direction matches the associated structure break
- volume_ratio >= 0 (never negative)
- mitigated OBs do NOT appear in active list
- detector is deterministic on same inputs
"""

from __future__ import annotations

import random

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st, settings as h_settings  # noqa: E402

from shared.models import Candle
from strategy_service.market_structure import MarketStructureAnalyzer
from strategy_service.order_blocks import OrderBlockDetector


def _candle(i: int, open_: float, close: float, high: float, low: float,
            vol: float = 10.0, tf: str = "15m") -> Candle:
    return Candle(
        timestamp=1_700_000_000_000 + i * 900_000,
        open=open_, close=close, high=high, low=low,
        volume=vol, volume_quote=vol * ((high + low) / 2),
        pair="BTC/USDT", timeframe=tf, confirmed=True,
    )


def _random_walk_with_impulse(rng: random.Random, n: int, base: float = 1000.0,
                              vol: float = 0.005) -> list[Candle]:
    """Random walk with occasional larger moves so BOS/CHoCH + OBs form."""
    candles = []
    price = base
    for i in range(n):
        # Occasional impulse candles (10% chance)
        impulse = rng.random() < 0.1
        vol_mult = 3.0 if impulse else 1.0
        ret = rng.gauss(0, vol * vol_mult)
        new_price = price * (1 + ret)
        hi = max(price, new_price) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(price, new_price) * (1 - abs(rng.gauss(0, vol / 3)))
        v = 20.0 * vol_mult * (1 + rng.random())
        candles.append(_candle(i, price, new_price, hi, lo, v))
        price = new_price
    return candles


def _run_detector(candles: list[Candle]) -> list:
    struct = MarketStructureAnalyzer()
    state = struct.analyze(candles, "BTC/USDT", "15m")
    detector = OrderBlockDetector()
    now_ms = candles[-1].timestamp + 60_000 if candles else 0
    return detector.update(
        candles, state.structure_breaks, "BTC/USDT", "15m", now_ms,
    )


class TestOrderBlockStructuralInvariants:
    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=80, deadline=None)
    def test_body_within_wick_bounds(self, seed):
        rng = random.Random(seed)
        candles = _random_walk_with_impulse(rng, 300, vol=0.008)
        obs = _run_detector(candles)
        for ob in obs:
            assert ob.low <= ob.body_low, (
                f"OB body_low {ob.body_low} below wick low {ob.low}"
            )
            assert ob.body_high <= ob.high, (
                f"OB body_high {ob.body_high} above wick high {ob.high}"
            )
            assert ob.body_low <= ob.body_high
            assert ob.low <= ob.high

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=80, deadline=None)
    def test_entry_is_body_midpoint(self, seed):
        rng = random.Random(seed)
        candles = _random_walk_with_impulse(rng, 300, vol=0.008)
        obs = _run_detector(candles)
        for ob in obs:
            expected_mid = (ob.body_low + ob.body_high) / 2
            assert ob.entry_price == pytest.approx(expected_mid, abs=1e-6), (
                f"entry_price {ob.entry_price} != body midpoint {expected_mid}"
            )

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=80, deadline=None)
    def test_direction_matches_associated_break(self, seed):
        rng = random.Random(seed)
        candles = _random_walk_with_impulse(rng, 300, vol=0.008)
        obs = _run_detector(candles)
        for ob in obs:
            assert ob.associated_break is not None
            # bullish OB → must come from bullish break (BOS up or CHoCH flip up)
            assert ob.direction == ob.associated_break.direction, (
                f"OB direction {ob.direction} mismatches break direction "
                f"{ob.associated_break.direction}"
            )

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=80, deadline=None)
    def test_volume_ratio_non_negative(self, seed):
        rng = random.Random(seed)
        candles = _random_walk_with_impulse(rng, 300, vol=0.008)
        obs = _run_detector(candles)
        for ob in obs:
            assert ob.volume_ratio >= 0.0, (
                f"volume_ratio {ob.volume_ratio} must be non-negative"
            )
            assert ob.volume >= 0.0

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=80, deadline=None)
    def test_active_obs_exclude_mitigated(self, seed):
        rng = random.Random(seed)
        candles = _random_walk_with_impulse(rng, 300, vol=0.008)
        obs = _run_detector(candles)
        # The public result of update() is the active list — mitigated must be filtered
        for ob in obs:
            assert ob.mitigated is False, (
                "update() must not return mitigated OBs in active list"
            )


class TestOrderBlockDeterminism:
    @given(seed=st.integers(min_value=0, max_value=10_000))
    @h_settings(max_examples=40, deadline=None)
    def test_same_candles_same_obs(self, seed):
        rng = random.Random(seed)
        candles = _random_walk_with_impulse(rng, 300, vol=0.008)
        a = _run_detector(candles)
        b = _run_detector(candles)
        assert len(a) == len(b)
        for x, y in zip(a, b):
            assert x.timestamp == y.timestamp
            assert x.direction == y.direction
            assert x.entry_price == pytest.approx(y.entry_price, abs=1e-9)
            assert x.body_low == pytest.approx(y.body_low, abs=1e-9)
            assert x.body_high == pytest.approx(y.body_high, abs=1e-9)
