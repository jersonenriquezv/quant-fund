"""
Tests for scripts/backtest_bootstrap.py and scripts/backtest_stability.py.

Uses REAL backtest CSV output (`backtest_results/*.csv`) to verify metrics.
No mocks. If a script's output drifts from reality, these break.

Brutality rules: exact values for point metrics, hypothesis property tests
for resample invariants, tight percentile bounds.
"""

from __future__ import annotations

import csv
import math
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backtest_bootstrap import (
    Trade as BTrade,
    bootstrap,
    load_trades,
    max_drawdown_from_sequence,
    percentile,
    profit_factor,
    total_pnl,
    win_rate,
)
from scripts.backtest_stability import (
    Trade as STrade,
    max_dd,
    pf as pf_stability,
    split_by_count,
    win_rate as wr_stability,
    load_trades as load_stability,
)


REAL_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backtest_results",
    "backtest_results_20260330_203441_pre_diagnostic.csv",
)


# ================================================================
# Point-metric math — hand-verified on constructed inputs
# ================================================================

class TestPointMetricsExact:
    def test_pf_only_wins(self):
        trades = [BTrade("B", "s", 10.0, "tp"), BTrade("B", "s", 5.0, "tp")]
        assert profit_factor(trades) == float("inf")

    def test_pf_only_losses(self):
        trades = [BTrade("B", "s", -10.0, "sl"), BTrade("B", "s", -5.0, "sl")]
        assert profit_factor(trades) == 0.0

    def test_pf_mixed_exact(self):
        trades = [
            BTrade("B", "s", 30.0, "tp"), BTrade("B", "s", 10.0, "tp"),
            BTrade("B", "s", -20.0, "sl"), BTrade("B", "s", -5.0, "sl"),
        ]
        # wins=40, losses=25, PF=40/25=1.6
        assert profit_factor(trades) == pytest.approx(1.6, abs=1e-9)

    def test_wr_excludes_flat_trades(self):
        trades = [
            BTrade("B", "s", 10.0, "tp"), BTrade("B", "s", -5.0, "sl"),
            BTrade("B", "s", 0.0, "breakeven"), BTrade("B", "s", 0.005, "breakeven"),
        ]
        # Flat ones excluded, 1 win / 2 decisive = 0.5
        assert win_rate(trades) == pytest.approx(0.5, abs=1e-9)

    def test_max_dd_simple(self):
        # eq: 10, 15, 5 (dd=10), 12, -3 (dd=18 from peak 15)
        trades = [
            BTrade("B", "s", 10.0, "tp"), BTrade("B", "s", 5.0, "tp"),
            BTrade("B", "s", -10.0, "sl"), BTrade("B", "s", 7.0, "tp"),
            BTrade("B", "s", -15.0, "sl"),
        ]
        assert max_drawdown_from_sequence(trades) == pytest.approx(18.0, abs=1e-9)

    def test_max_dd_all_gains(self):
        trades = [BTrade("B", "s", 10.0, "tp")] * 5
        assert max_drawdown_from_sequence(trades) == 0.0

    def test_total_pnl_exact(self):
        trades = [BTrade("B", "s", 1.23, "tp"), BTrade("B", "s", -4.56, "sl")]
        assert total_pnl(trades) == pytest.approx(-3.33, abs=1e-9)


# ================================================================
# Percentile
# ================================================================

class TestPercentile:
    def test_p50_of_sorted_range(self):
        arr = sorted(list(range(1, 101)))  # 1..100
        # With the inclusive percentile formula: idx = round(99 * 50 / 100) = 50
        # arr[50] = 51
        assert percentile(arr, 50) == 51

    def test_p5_lower_bound(self):
        arr = list(range(100))
        assert percentile(arr, 5) == 5

    def test_p95_upper_bound(self):
        arr = list(range(100))
        assert percentile(arr, 95) == 94

    def test_empty_array_returns_zero(self):
        assert percentile([], 50) == 0.0


# ================================================================
# Bootstrap determinism + invariants
# ================================================================

class TestBootstrapDeterminism:
    def test_same_seed_same_result(self):
        trades = [BTrade("B", "s", float(i - 50), "tp") for i in range(100)]
        a = bootstrap(trades, n_iter=200, seed=7)
        b = bootstrap(trades, n_iter=200, seed=7)
        assert a["pf"] == b["pf"]
        assert a["pnl"] == b["pnl"]

    def test_diff_seed_diff_result(self):
        trades = [BTrade("B", "s", float(i - 50), "tp") for i in range(100)]
        a = bootstrap(trades, n_iter=200, seed=1)
        b = bootstrap(trades, n_iter=200, seed=2)
        assert a["pf"] != b["pf"]

    def test_all_distributions_length_matches_iter(self):
        trades = [BTrade("B", "s", 1.0, "tp"), BTrade("B", "s", -0.5, "sl")]
        d = bootstrap(trades, n_iter=50, seed=0)
        assert len(d["pf"]) == 50
        assert len(d["wr"]) == 50
        assert len(d["pnl"]) == 50
        assert len(d["dd"]) == 50

    def test_percentile_ordering(self):
        trades = [BTrade("B", "s", float(i - 50), "tp") for i in range(200)]
        d = bootstrap(trades, n_iter=500, seed=0)
        for key in ("pf", "wr", "pnl", "dd"):
            arr = d[key]
            assert percentile(arr, 5) <= percentile(arr, 50)
            assert percentile(arr, 50) <= percentile(arr, 95)


# ================================================================
# Real CSV — load + compute
# ================================================================

@pytest.mark.skipif(not os.path.exists(REAL_CSV), reason="real backtest CSV not present")
class TestRealCSVMetrics:
    """Metrics on the pre-diagnostic 104-trade backtest.

    Values verified against manual SQL/pandas. If backtest format changes,
    these break — that is the point.
    """

    def test_load_trade_count(self):
        trades = load_trades(REAL_CSV)
        assert len(trades) == 104

    def test_total_pnl_matches_tracker(self):
        trades = load_trades(REAL_CSV)
        # TRACKER.md entry: PnL=-$717, PF=0.87, WR=36.5% for this file
        assert total_pnl(trades) == pytest.approx(-717.34, abs=0.50)

    def test_profit_factor_matches_tracker(self):
        trades = load_trades(REAL_CSV)
        assert profit_factor(trades) == pytest.approx(0.87, abs=0.01)

    def test_win_rate_matches_tracker(self):
        trades = load_trades(REAL_CSV)
        assert win_rate(trades) == pytest.approx(0.365, abs=0.01)

    def test_bootstrap_ci_contains_point_estimate(self):
        trades = load_trades(REAL_CSV)
        d = bootstrap(trades, n_iter=2000, seed=42)
        pf_point = profit_factor(trades)
        p5 = percentile(d["pf"], 5)
        p95 = percentile(d["pf"], 95)
        assert p5 <= pf_point <= p95, (
            f"point PF {pf_point} not in [{p5}, {p95}] — bootstrap bias bug"
        )


# ================================================================
# Stability split
# ================================================================

class TestStabilitySplit:
    def test_split_into_quartiles_equal_size(self):
        trades = [STrade(entry_ts=i, pair="B", setup_type="s", direction="long",
                         pnl_usd=1.0, exit_reason="tp") for i in range(100)]
        splits = split_by_count(trades, 4)
        assert len(splits) == 4
        assert all(len(s) == 25 for s in splits)

    def test_split_handles_remainder(self):
        trades = [STrade(entry_ts=i, pair="B", setup_type="s", direction="long",
                         pnl_usd=1.0, exit_reason="tp") for i in range(103)]
        splits = split_by_count(trades, 4)
        # First 3 windows get extra trade each, last gets rest: 26,26,26,25
        assert [len(s) for s in splits] == [26, 26, 26, 25]
        assert sum(len(s) for s in splits) == 103

    def test_max_dd_matches_bootstrap_version(self):
        """stability.max_dd and bootstrap.max_drawdown_from_sequence
        must produce the same result on identical inputs (shared meaning).
        """
        pnls = [10.0, 5.0, -10.0, 7.0, -15.0]
        b_trades = [BTrade("B", "s", p, "tp") for p in pnls]
        s_trades = [STrade(entry_ts=i, pair="B", setup_type="s",
                           direction="long", pnl_usd=p, exit_reason="tp")
                    for i, p in enumerate(pnls)]
        assert max_dd(s_trades) == max_drawdown_from_sequence(b_trades)


@pytest.mark.skipif(not os.path.exists(REAL_CSV), reason="real backtest CSV not present")
class TestRealCSVStability:
    def test_stability_detects_edge_concentration(self):
        """Real CSV has edge concentrated in first ~5 days (PF 3.25 window 1
        then collapses). Stability analysis must surface high CV."""
        trades = load_stability(REAL_CSV)
        assert len(trades) == 104
        splits = split_by_count(trades, 4)
        pfs = [pf_stability(s) for s in splits if s]
        assert len(pfs) == 4
        # Window 1 PF ~3.25, windows 2-4 all <1
        assert pfs[0] >= 2.5, f"first window should show golden period PF, got {pfs[0]}"
        for later in pfs[1:]:
            assert later < 1.5, f"later window should show collapsed edge PF, got {later}"

    def test_stability_wr_first_window_wins(self):
        trades = load_stability(REAL_CSV)
        splits = split_by_count(trades, 4)
        first_wr = wr_stability(splits[0])
        rest_wrs = [wr_stability(s) for s in splits[1:]]
        assert first_wr > 0.55, f"first window WR should be high, got {first_wr}"
        assert all(wr < 0.45 for wr in rest_wrs), (
            f"rest WRs should be low, got {rest_wrs}"
        )


# ================================================================
# Property tests
# ================================================================

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st, settings as h_settings  # noqa: E402


class TestBootstrapInvariants:
    @given(
        pnls=st.lists(
            st.floats(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False),
            min_size=5, max_size=200,
        )
    )
    @h_settings(max_examples=100, deadline=None)
    def test_total_pnl_equals_sum(self, pnls):
        trades = [BTrade("B", "s", p, "tp") for p in pnls]
        assert math.isclose(total_pnl(trades), sum(pnls), abs_tol=1e-6)

    @given(
        pnls=st.lists(
            st.floats(min_value=-100, max_value=100, allow_nan=False, allow_infinity=False),
            min_size=10, max_size=100,
        )
    )
    @h_settings(max_examples=50, deadline=None)
    def test_bootstrap_never_produces_impossible_wr(self, pnls):
        trades = [BTrade("B", "s", p, "tp") for p in pnls]
        d = bootstrap(trades, n_iter=30, seed=0)
        for w in d["wr"]:
            assert 0.0 <= w <= 1.0, f"win rate {w} out of [0,1]"

    @given(
        pnls=st.lists(
            st.floats(min_value=-100, max_value=100, allow_nan=False, allow_infinity=False),
            min_size=10, max_size=100,
        )
    )
    @h_settings(max_examples=50, deadline=None)
    def test_dd_is_non_negative(self, pnls):
        trades = [BTrade("B", "s", p, "tp") for p in pnls]
        assert max_drawdown_from_sequence(trades) >= 0.0
        d = bootstrap(trades, n_iter=20, seed=0)
        for dd in d["dd"]:
            assert dd >= 0.0, f"drawdown {dd} is negative"


# ================================================================
# Temp CSV round-trip
# ================================================================

class TestCsvRoundTrip:
    def test_load_trades_handles_malformed_rows(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["trade_id", "pair", "direction", "setup_type",
                             "entry_price", "sl_price", "tp1", "tp2",
                             "position_size", "leverage", "entry_time",
                             "close_time", "exit_price", "pnl_usd", "exit_reason"])
            writer.writerow([1, "BTC/USDT", "long", "setup_f",
                             100, 95, 105, 110, 1, 5,
                             "2026-04-01 00:00", "2026-04-01 01:00",
                             110, "10.50", "tp"])
            # Malformed — non-numeric pnl
            writer.writerow([2, "ETH/USDT", "short", "setup_b",
                             2000, 2050, 1950, 1900, 1, 5,
                             "2026-04-01 00:00", "2026-04-01 01:00",
                             1900, "not_a_number", "tp"])
            path = f.name

        try:
            trades = load_trades(path)
            assert len(trades) == 1, "malformed row must be silently skipped"
            assert trades[0].pnl_usd == pytest.approx(10.50, abs=1e-9)
        finally:
            os.unlink(path)
