"""
Tests for Engine 1 benchmarks — strategy_service/engines/benchmarks.py.

Covers:
- Determinism of the sha256 coin flip across calls / processes
- Random-direction bench mirrors SL/TP correctly when flipped
- Random-direction bench preserves geometry when not flipped
- Market-now bench math (long + short) and degenerate-input rejection
- emit_engine1_benchmarks helper co-emits via on_match callback
- setup_type widths fit the existing VARCHAR(40) ml_setups column
- Dedup keys differ across (engine1, bench_random, bench_market_now)
"""

import time
from unittest.mock import patch

import pytest

from shared.models import TradeSetup
from strategy_service.engines.benchmarks import (
    BENCH_MARKET_NOW,
    BENCH_RANDOM_DIRECTION,
    _coinflip,
    emit_engine1_benchmarks,
    make_market_now_bench,
    make_random_direction_bench,
)


# ============================================================
# Helpers
# ============================================================

def _engine1_setup(
    *,
    pair: str = "BTC/USDT",
    direction: str = "long",
    timestamp: int = 1_700_000_000_000,
    entry: float = 100.0,
    sl: float = 99.0,
    tp1: float = 101.0,
    tp2: float = 102.0,
) -> TradeSetup:
    return TradeSetup(
        timestamp=timestamp,
        pair=pair,
        direction=direction,
        setup_type="engine1_trend_pullback",
        entry_price=entry,
        sl_price=sl,
        tp1_price=tp1,
        tp2_price=tp2,
        confluences=["engine1_impulse_atr_2.50x"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
    )


# ============================================================
# Determinism / coin flip
# ============================================================

class TestCoinflip:
    def test_deterministic_repeated_calls(self):
        a = _coinflip("BTC/USDT", 1_700_000_000_000, "exp_x")
        b = _coinflip("BTC/USDT", 1_700_000_000_000, "exp_x")
        assert a == b

    def test_pair_changes_outcome(self):
        # Different pairs with the same timestamp / experiment_id MAY
        # disagree — assert we sample at least both outcomes across many.
        outcomes = {
            _coinflip(p, 1_700_000_000_000, "exp_x")
            for p in ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT"]
        }
        assert outcomes == {True, False}, (
            "sha256 coinflip should not be constant across pairs"
        )

    def test_experiment_id_changes_outcome(self):
        outcomes = {
            _coinflip("BTC/USDT", 1_700_000_000_000, exp)
            for exp in ["exp_a", "exp_b", "exp_c", "exp_d"]
        }
        assert outcomes == {True, False}

    def test_distribution_roughly_balanced(self):
        # 1000 distinct timestamps, same pair / experiment. sha256 is a
        # uniform PRF so ~50/50 ± a few percent.
        n = 1000
        flips = sum(
            1 for ts in range(n) if _coinflip("BTC/USDT", ts, "exp_x")
        )
        assert 400 <= flips <= 600, f"flips={flips} not within [400,600]"


# ============================================================
# bench_engine1_random_direction
# ============================================================

class TestRandomDirectionBench:
    def _force_flip(self, flip: bool):
        """Patch _coinflip to force a deterministic outcome for the test."""
        return patch(
            "strategy_service.engines.benchmarks._coinflip",
            return_value=flip,
        )

    def test_setup_type_is_bench(self):
        s = _engine1_setup()
        with self._force_flip(False):
            bench = make_random_direction_bench(s, experiment_id="exp_x")
        assert bench.setup_type == BENCH_RANDOM_DIRECTION

    def test_no_flip_preserves_geometry(self):
        s = _engine1_setup()  # long, entry 100, sl 99, tp1 101, tp2 102
        with self._force_flip(False):
            bench = make_random_direction_bench(s, experiment_id="exp_x")
        assert bench.direction == "long"
        assert bench.entry_price == s.entry_price
        assert bench.sl_price == s.sl_price
        assert bench.tp1_price == s.tp1_price
        assert bench.tp2_price == s.tp2_price

    def test_flip_mirrors_sl_tp(self):
        # entry=100, sl=99, tp1=101, tp2=102 -> flipped:
        #   sl  = 2*100-99  = 101
        #   tp1 = 2*100-101 = 99
        #   tp2 = 2*100-102 = 98
        s = _engine1_setup()
        with self._force_flip(True):
            bench = make_random_direction_bench(s, experiment_id="exp_x")
        assert bench.direction == "short"
        assert bench.entry_price == 100.0
        assert bench.sl_price == pytest.approx(101.0)
        assert bench.tp1_price == pytest.approx(99.0)
        assert bench.tp2_price == pytest.approx(98.0)

    def test_flip_preserves_rr(self):
        # R:R must be invariant under direction flip — this is the whole
        # point of the mirror.
        s = _engine1_setup(entry=100.0, sl=98.0, tp1=104.0, tp2=110.0)
        original_r = abs(s.entry_price - s.sl_price)
        original_tp1_r = abs(s.tp1_price - s.entry_price) / original_r
        original_tp2_r = abs(s.tp2_price - s.entry_price) / original_r

        with self._force_flip(True):
            bench = make_random_direction_bench(s, experiment_id="exp_x")
        new_r = abs(bench.entry_price - bench.sl_price)
        new_tp1_r = abs(bench.tp1_price - bench.entry_price) / new_r
        new_tp2_r = abs(bench.tp2_price - bench.entry_price) / new_r
        assert new_r == pytest.approx(original_r)
        assert new_tp1_r == pytest.approx(original_tp1_r)
        assert new_tp2_r == pytest.approx(original_tp2_r)

    def test_extra_features_recorded(self):
        s = _engine1_setup()
        with self._force_flip(True):
            bench = make_random_direction_bench(s, experiment_id="exp_x")
        assert bench.extra_features["bench_engine1_random_flip"] == 1
        assert bench.extra_features["bench_engine1_origin_direction"] == "long"


# ============================================================
# bench_engine1_market_now
# ============================================================

class TestMarketNowBench:
    def test_setup_type_is_bench(self):
        s = _engine1_setup()
        bench = make_market_now_bench(s, current_price=100.5)
        assert bench is not None
        assert bench.setup_type == BENCH_MARKET_NOW

    def test_long_geometry(self):
        # Engine 1: entry=100, sl=99, tp1=101, tp2=102 -> R=1, TP1=+1, TP2=+2
        # Market-now at current_price=100.5:
        #   entry=100.5, sl=99.5, tp1=101.5, tp2=102.5
        s = _engine1_setup()
        bench = make_market_now_bench(s, current_price=100.5)
        assert bench is not None
        assert bench.direction == "long"
        assert bench.entry_price == pytest.approx(100.5)
        assert bench.sl_price == pytest.approx(99.5)
        assert bench.tp1_price == pytest.approx(101.5)
        assert bench.tp2_price == pytest.approx(102.5)

    def test_short_geometry(self):
        # Engine 1 short: entry=100, sl=101, tp1=99, tp2=98 -> R=1, TP1=-1, TP2=-2
        # Market-now at current_price=99.7:
        #   entry=99.7, sl=100.7, tp1=98.7, tp2=97.7
        s = _engine1_setup(direction="short", entry=100.0, sl=101.0, tp1=99.0, tp2=98.0)
        bench = make_market_now_bench(s, current_price=99.7)
        assert bench is not None
        assert bench.direction == "short"
        assert bench.entry_price == pytest.approx(99.7)
        assert bench.sl_price == pytest.approx(100.7)
        assert bench.tp1_price == pytest.approx(98.7)
        assert bench.tp2_price == pytest.approx(97.7)

    def test_rr_preserved(self):
        s = _engine1_setup(entry=100.0, sl=98.0, tp1=104.0, tp2=110.0)
        bench = make_market_now_bench(s, current_price=99.5)
        assert bench is not None
        original_r = abs(s.entry_price - s.sl_price)
        original_tp2_r = abs(s.tp2_price - s.entry_price) / original_r
        new_r = abs(bench.entry_price - bench.sl_price)
        new_tp2_r = abs(bench.tp2_price - bench.entry_price) / new_r
        assert new_r == pytest.approx(original_r)
        assert new_tp2_r == pytest.approx(original_tp2_r)

    def test_returns_none_on_zero_price(self):
        s = _engine1_setup()
        assert make_market_now_bench(s, current_price=0.0) is None
        assert make_market_now_bench(s, current_price=-1.0) is None

    def test_returns_none_on_zero_sl_distance(self):
        # Pathological: entry == sl. Cannot derive a meaningful R.
        s = _engine1_setup(entry=100.0, sl=100.0, tp1=101.0, tp2=102.0)
        assert make_market_now_bench(s, current_price=100.5) is None

    def test_extra_features_record_offset(self):
        s = _engine1_setup()  # entry=100
        bench = make_market_now_bench(s, current_price=100.5)
        assert bench is not None
        assert bench.extra_features["bench_engine1_origin_entry"] == 100.0
        assert bench.extra_features["bench_engine1_origin_sl"] == 99.0
        # |100.5 - 100| / 100.5 ≈ 0.004975
        assert bench.extra_features[
            "bench_engine1_market_entry_offset_pct"
        ] == pytest.approx(0.5 / 100.5)


# ============================================================
# emit_engine1_benchmarks helper — co-emission contract
# ============================================================

class TestEmitEngine1Benchmarks:
    def _capture_callback(self):
        captured: list[TradeSetup] = []

        def on_match(setup: TradeSetup) -> bool:
            captured.append(setup)
            return False  # mimic evaluate_all() — never short-circuit

        return captured, on_match

    def test_emits_both_when_both_registered(self):
        captured, on_match = self._capture_callback()
        s = _engine1_setup()
        with patch(
            "strategy_service.engines.benchmarks.settings"
        ) as mock_settings:
            mock_settings.SHADOW_MODE_SETUPS = [
                "engine1_trend_pullback",
                BENCH_RANDOM_DIRECTION,
                BENCH_MARKET_NOW,
            ]
            mock_settings.EXPERIMENT_ID = "exp_x"
            short_circuited = emit_engine1_benchmarks(
                s, current_price=100.5, on_match=on_match,
            )
        assert short_circuited is False
        assert [b.setup_type for b in captured] == [
            BENCH_RANDOM_DIRECTION, BENCH_MARKET_NOW,
        ]

    def test_skips_disabled_benchmark(self):
        captured, on_match = self._capture_callback()
        s = _engine1_setup()
        with patch(
            "strategy_service.engines.benchmarks.settings"
        ) as mock_settings:
            mock_settings.SHADOW_MODE_SETUPS = [
                "engine1_trend_pullback",
                # Only the market-now bench registered; random-direction off.
                BENCH_MARKET_NOW,
            ]
            mock_settings.EXPERIMENT_ID = "exp_x"
            emit_engine1_benchmarks(
                s, current_price=100.5, on_match=on_match,
            )
        assert [b.setup_type for b in captured] == [BENCH_MARKET_NOW]

    def test_no_emission_when_neither_registered(self):
        captured, on_match = self._capture_callback()
        s = _engine1_setup()
        with patch(
            "strategy_service.engines.benchmarks.settings"
        ) as mock_settings:
            mock_settings.SHADOW_MODE_SETUPS = ["engine1_trend_pullback"]
            mock_settings.EXPERIMENT_ID = "exp_x"
            emit_engine1_benchmarks(
                s, current_price=100.5, on_match=on_match,
            )
        assert captured == []

    def test_skips_market_now_when_current_price_invalid(self):
        captured, on_match = self._capture_callback()
        s = _engine1_setup()
        with patch(
            "strategy_service.engines.benchmarks.settings"
        ) as mock_settings:
            mock_settings.SHADOW_MODE_SETUPS = [
                "engine1_trend_pullback",
                BENCH_RANDOM_DIRECTION,
                BENCH_MARKET_NOW,
            ]
            mock_settings.EXPERIMENT_ID = "exp_x"
            emit_engine1_benchmarks(
                s, current_price=0.0, on_match=on_match,
            )
        # Random-direction still emits (not price-dependent); market-now skipped.
        assert [b.setup_type for b in captured] == [BENCH_RANDOM_DIRECTION]


# ============================================================
# Schema + dedup safety
# ============================================================

class TestSchemaAndDedup:
    def test_setup_type_widths_fit_varchar_40(self):
        assert len(BENCH_RANDOM_DIRECTION) <= 40
        assert len(BENCH_MARKET_NOW) <= 40

    def test_dedup_keys_differ_from_engine1(self):
        # main.py dedup_key = (pair, direction, setup_type). All three
        # benchmarks must have a distinct setup_type so they never
        # suppress Engine 1 nor each other in the shared dedup cache.
        s = _engine1_setup()
        with patch(
            "strategy_service.engines.benchmarks._coinflip",
            return_value=False,
        ):
            bench_rd = make_random_direction_bench(s, experiment_id="exp_x")
        bench_mn = make_market_now_bench(s, current_price=100.5)
        assert bench_mn is not None
        keys = {
            (s.pair, s.direction, s.setup_type),
            (bench_rd.pair, bench_rd.direction, bench_rd.setup_type),
            (bench_mn.pair, bench_mn.direction, bench_mn.setup_type),
        }
        assert len(keys) == 3, f"dedup keys collide: {keys}"
