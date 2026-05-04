"""Sanity tests for scripts/report_scalp_shadow.py.

These tests exercise the metric math (`_compute_stats`) and the decision
rule (`_decision`) on hand-constructed inputs so the report can't drift
silently. No DB access — all rows are synthetic dicts shaped like
ml_setups rows.
"""

from datetime import datetime, timedelta
import math

import pytest

from scripts.report_scalp_shadow import (
    _BASELINE_TYPE,
    _compute_stats,
    _decision,
)


# ============================================================
# Helpers
# ============================================================

def _row(
    *,
    setup_type: str = "scalp_liq_reclaim_v1",
    pair: str = "BTC/USDT",
    direction: str = "long",
    outcome_type: str = "shadow_tp",
    pnl_pct: float | None = 0.004,
    pnl_usd: float = 4.0,
    trade_duration_ms: int = 60_000,
    fill_duration_ms: int = 1_000,
    created_at: datetime | None = None,
    resolved_at: datetime | None = None,
) -> dict:
    return {
        "setup_type": setup_type,
        "pair": pair,
        "direction": direction,
        "outcome_type": outcome_type,
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd,
        "trade_duration_ms": trade_duration_ms,
        "fill_duration_ms": fill_duration_ms,
        "created_at": created_at or datetime(2026, 5, 1, 12, 0, 0),
        "resolved_at": resolved_at or datetime(2026, 5, 1, 12, 1, 0),
    }


def _series(*, n_wins: int, n_losses: int, win_pnl_pct: float,
            loss_pnl_pct: float, span_days: float = 10.0,
            setup_type: str = "scalp_liq_reclaim_v1") -> list[dict]:
    """Build n_wins + n_losses rows spread evenly across span_days."""
    total = n_wins + n_losses
    if total == 0:
        return []
    base = datetime(2026, 5, 1, 0, 0, 0)
    step = timedelta(days=span_days) / total
    rows: list[dict] = []
    for i in range(n_wins):
        rows.append(_row(
            setup_type=setup_type,
            outcome_type="shadow_tp", pnl_pct=win_pnl_pct, pnl_usd=4.0,
            created_at=base + step * i,
        ))
    for i in range(n_losses):
        rows.append(_row(
            setup_type=setup_type,
            outcome_type="shadow_sl", pnl_pct=loss_pnl_pct, pnl_usd=-2.0,
            created_at=base + step * (n_wins + i),
        ))
    return rows


# ============================================================
# _compute_stats
# ============================================================

class TestComputeStats:

    def test_returns_none_for_empty_input(self):
        assert _compute_stats("scalp_liq_reclaim_v1", [], fee_fraction=0.0011) is None

    def test_skips_rows_with_null_pnl(self):
        rows = [_row(pnl_pct=None), _row(pnl_pct=None)]
        assert _compute_stats("x", rows, fee_fraction=0.0) is None

    def test_basic_wr_and_pf_no_fees(self):
        rows = _series(n_wins=60, n_losses=40,
                       win_pnl_pct=0.004, loss_pnl_pct=-0.002)
        s = _compute_stats("scalp_liq_reclaim_v1", rows, fee_fraction=0.0)
        assert s is not None
        assert s.n == 100
        assert s.wins == 60
        assert s.losses == 40
        assert s.flat == 0
        assert s.wr_raw == pytest.approx(0.60)
        assert s.wr_post_fees == pytest.approx(0.60)
        # PF = (60 * 0.004) / (40 * 0.002) = 0.24 / 0.08 = 3.0
        assert s.pf_raw == pytest.approx(3.0)
        assert s.pf_post_fees == pytest.approx(3.0)

    def test_fees_shrink_pf_but_preserve_wr_when_wins_remain_positive(self):
        # 0.40% wins, 0.20% losses, 0.11% fee per round trip.
        # Wins post-fees: 0.40 - 0.11 = 0.29% (still positive → still wins).
        # Losses post-fees: -0.20 - 0.11 = -0.31% (still losses).
        rows = _series(n_wins=60, n_losses=40,
                       win_pnl_pct=0.004, loss_pnl_pct=-0.002)
        s = _compute_stats("x", rows, fee_fraction=0.0011)
        assert s is not None
        assert s.wr_post_fees == pytest.approx(0.60)
        # PF = (60 * 0.0029) / (40 * 0.0031) ≈ 1.4032
        expected_pf = (60 * 0.0029) / (40 * 0.0031)
        assert s.pf_post_fees == pytest.approx(expected_pf, rel=1e-3)
        # Raw PF unchanged.
        assert s.pf_raw == pytest.approx(3.0)

    def test_fees_can_flip_marginal_wins_to_losses(self):
        # 0.05% wins, well below the 0.11% fee → all wins become losses.
        rows = _series(n_wins=60, n_losses=40,
                       win_pnl_pct=0.0005, loss_pnl_pct=-0.002)
        s = _compute_stats("x", rows, fee_fraction=0.0011)
        assert s is not None
        # Post-fees wins = 0.0005 - 0.0011 = -0.0006 → all 60 became losses.
        assert s.wins == 0
        assert s.losses == 100
        assert s.wr_post_fees == pytest.approx(0.0)

    def test_avg_pnl_pct_is_post_fees_in_percent_units(self):
        # 50 wins of +0.005 + 50 losses of -0.005, fee 0.0011.
        # Avg pnl_pct fraction = (50*(0.005-0.0011) + 50*(-0.005-0.0011)) / 100
        #                     = (50*0.0039 + 50*-0.0061) / 100
        #                     = (0.195 - 0.305) / 100 = -0.0011
        # As percent: -0.11
        rows = _series(n_wins=50, n_losses=50,
                       win_pnl_pct=0.005, loss_pnl_pct=-0.005)
        s = _compute_stats("x", rows, fee_fraction=0.0011)
        assert s is not None
        assert s.avg_pnl_pct == pytest.approx(-0.11, rel=1e-3)

    def test_freq_per_day_uses_min_one_hour_span(self):
        """Single-row sample defaults span to 1 day so freq isn't divide-by-zero."""
        rows = [_row(pnl_pct=0.004)]
        s = _compute_stats("x", rows, fee_fraction=0.0)
        assert s is not None
        assert s.freq_per_day == pytest.approx(1.0)

    def test_freq_per_day_from_evenly_spaced_rows(self):
        # 10 rows over exactly 10 days → 1 row/day.
        rows = _series(n_wins=10, n_losses=0,
                       win_pnl_pct=0.004, loss_pnl_pct=-0.002, span_days=10.0)
        s = _compute_stats("x", rows, fee_fraction=0.0)
        assert s is not None
        # span_days here is the gap between first and last; with 10 rows
        # spread linearly the last sits at 9*step = 9 days. Freq = 10/9 ≈ 1.11.
        assert s.freq_per_day == pytest.approx(10 / 9, rel=1e-3)

    def test_pf_inf_when_no_losses(self):
        rows = _series(n_wins=20, n_losses=0,
                       win_pnl_pct=0.004, loss_pnl_pct=-0.002)
        s = _compute_stats("x", rows, fee_fraction=0.0)
        assert s is not None
        assert math.isinf(s.pf_raw)
        # With fees applied, wins still positive → still no losses, still inf.
        assert math.isinf(s.pf_post_fees)

    def test_avg_trade_duration_min(self):
        rows = []
        for ms in (60_000, 120_000, 180_000):  # 1, 2, 3 minutes
            rows.append(_row(trade_duration_ms=ms))
        s = _compute_stats("x", rows, fee_fraction=0.0)
        assert s is not None
        assert s.avg_trade_duration_min == pytest.approx(2.0)


# ============================================================
# _decision
# ============================================================

def _stats(**overrides):
    """Build a SignalStats with passing defaults; tests override one knob."""
    from scripts.report_scalp_shadow import SignalStats
    defaults = dict(
        setup_type="scalp_liq_reclaim_v1",
        n=200, wins=130, losses=70, flat=0,
        wr_raw=0.65, wr_post_fees=0.65,
        pf_raw=2.0, pf_post_fees=2.0,
        avg_pnl_pct=0.5,
        avg_trade_duration_min=3.0,
        freq_per_day=10.0,
        window_days=20.0,
    )
    defaults.update(overrides)
    return SignalStats(**defaults)


class TestDecision:

    def test_pass_when_all_rules_clear(self):
        passed, fails = _decision(_stats(), baseline_wr=0.40)
        assert passed is True
        assert fails == []

    def test_fail_on_low_n(self):
        passed, fails = _decision(_stats(n=99), baseline_wr=0.40)
        assert passed is False
        assert any("N=99" in f for f in fails)

    def test_fail_on_low_wr(self):
        passed, fails = _decision(_stats(wr_post_fees=0.50), baseline_wr=0.40)
        assert passed is False
        assert any("WR_post_fees" in f for f in fails)

    def test_fail_on_low_pf(self):
        passed, fails = _decision(_stats(pf_post_fees=1.4), baseline_wr=0.40)
        assert passed is False
        assert any("PF_post_fees" in f for f in fails)

    def test_inf_pf_passes_pf_check(self):
        passed, fails = _decision(_stats(pf_post_fees=math.inf), baseline_wr=0.40)
        assert passed is True
        assert fails == []

    def test_fail_when_baseline_delta_under_15pp(self):
        # 65% vs 55% baseline = +10pp, under threshold.
        passed, fails = _decision(_stats(wr_post_fees=0.65), baseline_wr=0.55)
        assert passed is False
        assert any("baseline" in f for f in fails)

    def test_pass_when_baseline_unavailable_skips_delta_rule(self):
        # No baseline → that rule is skipped, others must still hold.
        passed, fails = _decision(_stats(), baseline_wr=None)
        assert passed is True
        assert fails == []

    def test_fail_on_low_frequency(self):
        passed, fails = _decision(_stats(freq_per_day=4.99), baseline_wr=0.40)
        assert passed is False
        assert any("freq_per_day" in f for f in fails)

    def test_multiple_failures_listed(self):
        passed, fails = _decision(
            _stats(n=50, wr_post_fees=0.40, pf_post_fees=1.0, freq_per_day=2.0),
            baseline_wr=0.40,
        )
        assert passed is False
        # Five rules fail simultaneously (N, WR, PF, baseline-delta, freq).
        # None should be silently dropped — every failure must be reported.
        assert len(fails) == 5
