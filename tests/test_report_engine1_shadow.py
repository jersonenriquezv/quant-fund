"""Tests for scripts/report_engine1_shadow.py — detection-pass pairing logic.

Covers the report-side cohort filter that compensates for the (pair,
direction, setup_type) pipeline dedup key in main.py letting
direction-flipping benchmarks dodge dedup that would suppress Engine 1
itself. The report partitions bench rows into paired vs orphan by
clustering same-pair rows whose `created_at` falls within
`PAIRING_WINDOW_SECONDS`.

`setup.timestamp` is anchored to a structural feature (impulse origin,
OB candle, swing) so it can repeat across many detection passes — it is
NOT a trigger identity. `created_at` proximity is what binds engine1 to
its co-emitted benchmarks.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# `scripts/` is not an installed package — make it importable as a
# namespace package by putting the repo root on sys.path.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts.report_engine1_shadow as report
from scripts.report_engine1_shadow import (
    DRIFT_TOLERANCE_ROWS,
    ENGINE1_PRIMARY,
    JITTER_GAP_TOLERANCE_ROWS,
    N_TARGET,
    ORPHAN_EXPLODE_ROWS,
    ORPHAN_TOLERANCE_ROWS,
    PAIRING_WINDOW_SECONDS,
    PENDING_STALE_HOURS,
    Row,
    aggregate,
    aggregate_paired,
    classify_bench,
    cluster_passes,
    coemission_drift_effective,
    coemission_drift_paired,
    compute_verdict,
    counts_from_rows,
    jitter_gaps,
    orphan_counts,
)


BENCH_RANDOM = "bench_engine1_random_direction"
BENCH_MARKET = "bench_engine1_market_now"
T0 = datetime(2026, 4, 30, 0, 0, 0)


@pytest.fixture(autouse=True)
def _legacy_expected_pairs(monkeypatch):
    """Unit fixtures use BTC synthetic rows unless a test overrides scope."""
    monkeypatch.setattr(report, "EXPECTED_PAIRS", ("BTC/USDT", "ETH/USDT"))


def _row(setup: str, pair: str, ts: int, outcome: str, ca: datetime) -> Row:
    return (setup, pair, ts, outcome, ca)


def _pass(*, pair: str, anchor_ts: int, base: datetime, items: list[tuple[str, str]]) -> list[Row]:
    """Build a synthetic detection pass — rows separated by 200ms within the window."""
    return [
        _row(setup, pair, anchor_ts, outcome, base + timedelta(milliseconds=200 * i))
        for i, (setup, outcome) in enumerate(items)
    ]


# ============================================================
# Cluster passes
# ============================================================

def test_cluster_passes_groups_within_window():
    rows = _pass(
        pair="BTC/USDT", anchor_ts=1000, base=T0,
        items=[
            (ENGINE1_PRIMARY, "shadow_tp"),
            (BENCH_RANDOM, "shadow_sl"),
            (BENCH_MARKET, "shadow_breakeven"),
        ],
    )
    passes = cluster_passes(rows)
    assert len(passes) == 1
    assert len(passes[0]) == 3


def test_cluster_passes_splits_when_gap_exceeds_window():
    rows = [
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", T0),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_tp",
             T0 + timedelta(seconds=PAIRING_WINDOW_SECONDS + 0.5)),
    ]
    passes = cluster_passes(rows)
    assert len(passes) == 2


def test_cluster_passes_groups_at_exact_window_boundary():
    """created_at exactly PAIRING_WINDOW_SECONDS apart stays in ONE cluster.

    The clustering comparison is `<=` (report:231), so a row landing on
    the boundary belongs to the current pass. Pins the off-by-one: the
    split test uses +0.5 (above the window); this pins the inclusive edge.
    """
    rows = [
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", T0),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_tp",
             T0 + timedelta(seconds=PAIRING_WINDOW_SECONDS)),
    ]
    passes = cluster_passes(rows)
    assert len(passes) == 1
    assert len(passes[0]) == 2


def test_cluster_passes_separates_pairs():
    """Rows on different pairs never cluster together regardless of created_at."""
    rows = [
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", T0),
        _row(BENCH_RANDOM, "ETH/USDT", 1000, "shadow_tp", T0),
    ]
    passes = cluster_passes(rows)
    assert len(passes) == 2


# ============================================================
# Paired vs orphan classification
# ============================================================

def test_random_bench_direction_flip_creates_orphan_under_dedup():
    """Reproduce the failure mode the cohort filter exists to detect.

    Two consecutive triggers at T1 and T2 on BTC.

    Engine 1: emits short at both. Pipeline dedup keys
    (BTC, short, engine1) — T1 emits a row, T2 hits dedup TTL so no row.

    Bench random_direction: at T1 the sha256 flip says "long", at T2 it
    says "short". Pipeline dedup keys differ between calls
    ((BTC, long, bench_random) vs (BTC, short, bench_random)) so both
    rows land in ml_setups.

    Expected: bench has a paired row at T1 (engine1 row also exists at
    T1) and an orphan row at T2 (no engine1 row at T2 because pipeline
    dedup'd it).
    """
    t1 = T0
    t2 = T0 + timedelta(minutes=5)
    rows = [
        # Pass 1 at t1: engine1 short emitted, bench random long (flip on)
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", t1),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_sl", t1 + timedelta(milliseconds=300)),
        # Pass 2 at t2: engine1 short pipeline-deduped (no row), bench
        # random short (flip off) emits because its dedup key differs
        _row(BENCH_RANDOM, "BTC/USDT", 2000, "shadow_tp", t2),
    ]
    passes = cluster_passes(rows)
    classification = classify_bench(rows, passes)

    paired = classification[BENCH_RANDOM]["paired"]
    orphan = classification[BENCH_RANDOM]["orphan"]

    assert len(paired) == 1
    assert paired[0][2] == 1000  # T1 paired
    assert len(orphan) == 1
    assert orphan[0][2] == 2000  # T2 orphan


def test_paired_only_aggregate_excludes_orphan():
    """Paired aggregate must not count orphan rows toward in-scope emissions."""
    t1 = T0
    t2 = T0 + timedelta(minutes=5)
    t3 = T0 + timedelta(minutes=10)
    rows = [
        # Pass 1: paired
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", t1),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_tp", t1 + timedelta(milliseconds=300)),
        # Pass 2: orphan (no engine1)
        _row(BENCH_RANDOM, "BTC/USDT", 2000, "shadow_sl", t2),
        # Pass 3: orphan
        _row(BENCH_RANDOM, "BTC/USDT", 3000, "shadow_breakeven", t3),
    ]
    passes = cluster_passes(rows)
    classification = classify_bench(rows, passes)
    paired_agg = aggregate_paired(classification)

    raw = aggregate(counts_from_rows(rows))
    assert raw[BENCH_RANDOM]["in_scope_emissions"] == 3

    assert paired_agg[BENCH_RANDOM]["in_scope_emissions"] == 1
    assert paired_agg[BENCH_RANDOM]["tp"] == 1
    assert paired_agg[BENCH_RANDOM]["sl"] == 0
    assert paired_agg[BENCH_RANDOM]["be"] == 0
    assert orphan_counts(classification)[BENCH_RANDOM] == 2


def test_paired_drift_excludes_orphan_rows():
    """coemission_drift_paired uses paired emissions only.

    Engine 1 has 1 in-scope row paired with 1 bench row. Bench random
    has 5 additional orphan rows. Raw drift would be +5; paired drift
    is 0.
    """
    t1 = T0
    rows = [
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", t1),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_tp", t1 + timedelta(milliseconds=300)),
    ]
    # Five orphan bench rows separated far enough to form their own clusters
    for i in range(5):
        rows.append(
            _row(BENCH_RANDOM, "BTC/USDT", 2000 + i,
                 "shadow_sl", t1 + timedelta(minutes=5 * (i + 1))),
        )
    passes = cluster_passes(rows)
    classification = classify_bench(rows, passes)
    paired_agg = aggregate_paired(classification)
    raw_agg = aggregate(counts_from_rows(rows))
    e1_emis = raw_agg[ENGINE1_PRIMARY]["in_scope_emissions"]

    paired_drift = coemission_drift_paired(e1_emis, paired_agg)
    assert paired_drift[BENCH_RANDOM] == 0

    orphans = orphan_counts(classification)
    assert orphans[BENCH_RANDOM] == 5


def test_pair_filtered_engine1_in_pass_does_not_count_as_paired():
    """If the only Engine 1 row in a pass is shadow_pair_filtered, bench rows
    in that pass are orphan — pair_filtered engine1 is quarantine, not in-scope."""
    t1 = T0
    rows = [
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_pair_filtered", t1),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_tp",
             t1 + timedelta(milliseconds=300)),
    ]
    passes = cluster_passes(rows)
    classification = classify_bench(rows, passes)
    assert orphan_counts(classification)[BENCH_RANDOM] == 1
    assert classification[BENCH_RANDOM]["paired"] == []


# ============================================================
# Verdict
# ============================================================

def _agg_with(*, e1_emis: int, e1_resolved: int) -> dict[str, dict[str, int]]:
    """Build a minimal raw-agg fixture that satisfies compute_verdict's needs."""
    base: dict[str, dict[str, int]] = {}
    for s in (ENGINE1_PRIMARY, BENCH_RANDOM, BENCH_MARKET):
        base[s] = {
            "total": 0, "resolved": 0, "tp": 0, "sl": 0, "be": 0, "to": 0,
            "dedup": 0, "pair_filtered": 0, "pending": 0,
            "in_scope_emissions": 0,
        }
    base[ENGINE1_PRIMARY]["resolved"] = e1_resolved
    base[ENGINE1_PRIMARY]["in_scope_emissions"] = e1_emis
    base[ENGINE1_PRIMARY]["tp"] = e1_resolved
    return base


def test_verdict_orphan_drift_only_does_not_trigger_roto():
    """Status changes from ROTO to SOSPECHOSO when the only drift is orphans.

    Setup: paired drift is 0, but orphan rows exceed tolerance (3 > 2)
    while staying below the explode threshold. Old logic (raw drift)
    would have flagged ROTO because raw bench emissions outnumber Engine
    1. New logic separates them: paired drift drives ROTO, orphan rows
    drive SOSPECHOSO.
    """
    agg = _agg_with(e1_emis=10, e1_resolved=60)  # not sample-starved
    paired_drift = {BENCH_RANDOM: 0, BENCH_MARKET: 0}  # paired equal
    orphans = {BENCH_RANDOM: ORPHAN_TOLERANCE_ROWS + 1, BENCH_MARKET: 0}

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=paired_drift,
        gaps={BENCH_RANDOM: 0, BENCH_MARKET: 0},
        orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "SOSPECHOSO"
    assert exit_code == 1
    assert not roto
    assert any("orphan" in r for r in sospechoso)
    # Negative assertion: orphan is the ONLY reason — no spurious dedup /
    # jitter reason leaked in (agg has zero dedup, gaps all zero).
    assert len(sospechoso) == 1


def test_verdict_orphan_within_tolerance_yields_sano():
    """Below tolerance + paired drift 0 + sample met → SANO."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    paired_drift = {BENCH_RANDOM: 0, BENCH_MARKET: 0}
    orphans = {BENCH_RANDOM: ORPHAN_TOLERANCE_ROWS, BENCH_MARKET: 0}

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=paired_drift,
        gaps={BENCH_RANDOM: 0, BENCH_MARKET: 0},
        orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "SANO"
    assert exit_code == 0
    assert not roto and not sospechoso


def test_verdict_orphan_explode_triggers_roto():
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    paired_drift = {BENCH_RANDOM: 0, BENCH_MARKET: 0}
    orphans = {BENCH_RANDOM: ORPHAN_EXPLODE_ROWS + 1, BENCH_MARKET: 0}

    status, exit_code, roto, _ = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=paired_drift,
        gaps={BENCH_RANDOM: 0, BENCH_MARKET: 0},
        orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "ROTO"
    assert exit_code == 2
    assert any("orphan" in r and "explode" in r for r in roto)


def test_verdict_paired_drift_above_tolerance_triggers_roto():
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    paired_drift = {BENCH_RANDOM: DRIFT_TOLERANCE_ROWS + 1, BENCH_MARKET: 0}
    orphans = {BENCH_RANDOM: 0, BENCH_MARKET: 0}

    status, exit_code, roto, _ = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=paired_drift,
        gaps={BENCH_RANDOM: 0, BENCH_MARKET: 0},
        orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "ROTO"
    assert exit_code == 2
    assert any("effective drift" in r for r in roto)


def test_jitter_gaps_counts_engine1_passes_with_missing_bench():
    """Pass with engine1 in-scope and one bench missing increments that bench's gap."""
    t1 = T0
    t2 = T0 + timedelta(minutes=5)
    rows = [
        # Pass 1: engine1 + both benches
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", t1),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_tp", t1 + timedelta(milliseconds=300)),
        _row(BENCH_MARKET, "BTC/USDT", 1000, "shadow_tp", t1 + timedelta(milliseconds=600)),
        # Pass 2: engine1 + bench_random only — bench_market dedup'd at TTL boundary
        _row(ENGINE1_PRIMARY, "BTC/USDT", 2000, "shadow_dedup", t2),
        _row(BENCH_RANDOM, "BTC/USDT", 2000, "shadow_dedup", t2 + timedelta(milliseconds=300)),
    ]
    passes = cluster_passes(rows)
    gaps = jitter_gaps(passes)
    assert gaps[BENCH_MARKET] == 1
    assert gaps[BENCH_RANDOM] == 0


def test_jitter_gaps_ignores_passes_without_engine1_in_scope():
    """Bench-only passes (orphan passes) do not count as jitter gaps."""
    t1 = T0
    t2 = T0 + timedelta(minutes=5)
    rows = [
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_tp", t1),
        _row(BENCH_RANDOM, "BTC/USDT", 1000, "shadow_tp", t1 + timedelta(milliseconds=300)),
        _row(BENCH_MARKET, "BTC/USDT", 1000, "shadow_tp", t1 + timedelta(milliseconds=600)),
        # Orphan pass — no engine1 row at all
        _row(BENCH_RANDOM, "BTC/USDT", 2000, "shadow_tp", t2),
    ]
    passes = cluster_passes(rows)
    gaps = jitter_gaps(passes)
    assert gaps[BENCH_RANDOM] == 0
    assert gaps[BENCH_MARKET] == 0


def test_jitter_gaps_skips_pair_filtered_engine1():
    """Pass whose only engine1 row is shadow_pair_filtered is not in-scope, no gap counted."""
    t1 = T0
    rows = [
        _row(ENGINE1_PRIMARY, "BTC/USDT", 1000, "shadow_pair_filtered", t1),
    ]
    passes = cluster_passes(rows)
    gaps = jitter_gaps(passes)
    assert gaps[BENCH_RANDOM] == 0
    assert gaps[BENCH_MARKET] == 0


def test_effective_drift_zeros_out_when_jitter_gaps_explain_paired_negative():
    """If paired drift is -3 and jitter gaps for that bench are 3, effective drift = 0."""
    paired_agg = {
        BENCH_RANDOM: {"in_scope_emissions": 10},
        BENCH_MARKET: {"in_scope_emissions": 7},  # 3 less than engine1's 10
    }
    gaps = {BENCH_RANDOM: 0, BENCH_MARKET: 3}
    effective = coemission_drift_effective(10, paired_agg, gaps)
    assert effective[BENCH_RANDOM] == 0
    assert effective[BENCH_MARKET] == 0


def test_verdict_jitter_gaps_within_tolerance_keep_sano():
    """Bench paired underemits, jitter gaps fully explain, drift effective = 0 → SANO."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    paired_drift = {BENCH_RANDOM: 0, BENCH_MARKET: -2}
    drift_effective = {BENCH_RANDOM: 0, BENCH_MARKET: 0}
    # at tolerance, not exceeding — reference the constant so the boundary
    # tracks JITTER_GAP_TOLERANCE_ROWS if it ever changes.
    gaps = {BENCH_RANDOM: 0, BENCH_MARKET: JITTER_GAP_TOLERANCE_ROWS}
    orphans = {BENCH_RANDOM: 0, BENCH_MARKET: 0}

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=drift_effective,
        gaps=gaps, orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "SANO"
    assert not roto and not sospechoso


def test_verdict_jitter_gaps_above_tolerance_drive_sospechoso_not_roto():
    """User directive: TTL-jitter gaps must NOT trigger ROTO, only SOSPECHOSO."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    paired_drift = {BENCH_RANDOM: 0, BENCH_MARKET: -3}
    drift_effective = {BENCH_RANDOM: 0, BENCH_MARKET: 0}  # gaps fully explain
    gaps = {BENCH_RANDOM: 0, BENCH_MARKET: JITTER_GAP_TOLERANCE_ROWS + 1}
    orphans = {BENCH_RANDOM: 0, BENCH_MARKET: 0}

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=drift_effective,
        gaps=gaps, orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "SOSPECHOSO"
    assert exit_code == 1
    assert not roto
    assert any("jitter gaps" in r for r in sospechoso)


def test_verdict_paired_drift_unexplained_by_jitter_triggers_roto():
    """If paired drift is -5 but jitter gaps are only 2, effective drift = -3 → ROTO."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    paired_drift = {BENCH_RANDOM: 0, BENCH_MARKET: -5}
    drift_effective = {BENCH_RANDOM: 0, BENCH_MARKET: -3}  # > tol
    gaps = {BENCH_RANDOM: 0, BENCH_MARKET: 2}
    orphans = {BENCH_RANDOM: 0, BENCH_MARKET: 0}

    status, exit_code, roto, _ = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=drift_effective,
        gaps=gaps, orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "ROTO"
    assert any("effective drift" in r for r in roto)


def test_verdict_sample_starved_with_no_other_issues():
    agg = _agg_with(e1_emis=5, e1_resolved=10)  # below N_TARGET
    paired_drift = {BENCH_RANDOM: 0, BENCH_MARKET: 0}
    orphans = {BENCH_RANDOM: 0, BENCH_MARKET: 0}

    status, exit_code, _, _ = compute_verdict(
        agg=agg, drift_paired=paired_drift, drift_effective=paired_drift,
        gaps={BENCH_RANDOM: 0, BENCH_MARKET: 0},
        orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "SAMPLE-STARVED"
    assert exit_code == 0


# ============================================================
# Leak + pending ROTO arms — previously zero coverage. Every other
# verdict test passes leaks=[] and pending_max_age_h=None, so these two
# arms (report:392, :408) were never exercised: a bug in either would
# leave the whole suite green while the report emits a false SANO.
# ============================================================

_ZERO = {BENCH_RANDOM: 0, BENCH_MARKET: 0}


def test_verdict_pair_leakage_triggers_roto():
    """A non-empty leak group drives ROTO (rows outside expected pairs that
    are not shadow_pair_filtered = quarantine breach)."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)  # not sample-starved
    leaks = [(ENGINE1_PRIMARY, "DOGE/USDT", "shadow_tp", 3)]

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=_ZERO, drift_effective=_ZERO,
        gaps=_ZERO, orphans=_ZERO, leaks=leaks, pending_max_age_h=None,
    )
    assert status == "ROTO"
    assert exit_code == 2
    assert any("leakage" in r for r in roto)
    assert not sospechoso  # leak is the only reason


def test_verdict_stale_pending_triggers_roto():
    """Oldest pending row beyond PENDING_STALE_HOURS drives ROTO."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)

    status, exit_code, roto, _ = compute_verdict(
        agg=agg, drift_paired=_ZERO, drift_effective=_ZERO,
        gaps=_ZERO, orphans=_ZERO, leaks=[],
        pending_max_age_h=PENDING_STALE_HOURS + 1,
    )
    assert status == "ROTO"
    assert exit_code == 2
    assert any("stale pending" in r for r in roto)


def test_verdict_pending_at_stale_boundary_no_roto():
    """Pending age exactly at PENDING_STALE_HOURS does NOT trip ROTO (`>`).

    Pins the inclusive edge of the stale-pending arm so a `>`-to-`>=`
    refactor would fail loudly.
    """
    agg = _agg_with(e1_emis=10, e1_resolved=60)

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=_ZERO, drift_effective=_ZERO,
        gaps=_ZERO, orphans=_ZERO, leaks=[],
        pending_max_age_h=float(PENDING_STALE_HOURS),
    )
    assert status == "SANO"
    assert not roto and not sospechoso


# ============================================================
# Precedence + bucket-assignment guards. The verdict is built by
# accumulating into roto_reasons / sospechoso_reasons, then returning on
# the first non-empty bucket in the order ROTO > SOSPECHOSO >
# SAMPLE-STARVED > SANO (report:428). Two distinct things must hold and
# each needs its own test:
#   (a) ORDER  — a ROTO-level signal wins over a lower-severity state.
#   (b) BUCKET — a ROTO-level signal actually lands in roto_reasons, not
#       sospechoso_reasons. Testing order alone would let a refactor that
#       misfiles a leak into sospechoso pass silently.
# ============================================================

def test_verdict_orphan_explode_beats_sample_starved():
    """ORDER guard: a sample-starved run with orphan-explode reports ROTO,
    never SAMPLE-STARVED. Starvation can never mask a structural break
    because roto_reasons is evaluated before sample_starved."""
    agg = _agg_with(e1_emis=5, e1_resolved=10)  # starved (< N_TARGET)
    orphans = {BENCH_RANDOM: ORPHAN_EXPLODE_ROWS + 1, BENCH_MARKET: 0}

    status, exit_code, roto, _ = compute_verdict(
        agg=agg, drift_paired=_ZERO, drift_effective=_ZERO,
        gaps=_ZERO, orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "ROTO"
    assert exit_code == 2
    assert any("explode" in r for r in roto)
    assert agg[ENGINE1_PRIMARY]["resolved"] < N_TARGET  # genuinely starved


def test_verdict_leak_lands_in_roto_bucket_not_sospechoso():
    """BUCKET guard: pair leakage must populate roto_reasons, not
    sospechoso_reasons. Co-fire a sospechoso-level orphan count so BOTH
    buckets are non-empty — verifies the leak drives ROTO by bucket
    placement, not merely by evaluation order. A refactor moving leak
    detection into the sospechoso bucket would still satisfy an
    order-only test, so this asserts the leak reason is absent from
    sospechoso and present in roto."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    orphans = {BENCH_RANDOM: ORPHAN_TOLERANCE_ROWS + 1, BENCH_MARKET: 0}  # sospechoso-level
    leaks = [(ENGINE1_PRIMARY, "DOGE/USDT", "shadow_tp", 1)]

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=_ZERO, drift_effective=_ZERO,
        gaps=_ZERO, orphans=orphans, leaks=leaks, pending_max_age_h=None,
    )
    assert status == "ROTO"
    assert exit_code == 2
    assert any("leakage" in r for r in roto)          # leak in ROTO bucket
    assert not any("leakage" in r for r in sospechoso)  # not misfiled
    assert any("orphan" in r for r in sospechoso)     # sospechoso still recorded


def test_verdict_paired_drift_and_orphan_both_fire_reports_roto():
    """COMBINED guard: effective drift (ROTO) + orphan-tolerance
    (SOSPECHOSO) firing together reports ROTO and records both reasons in
    their respective buckets."""
    agg = _agg_with(e1_emis=10, e1_resolved=60)
    drift_effective = {BENCH_RANDOM: DRIFT_TOLERANCE_ROWS + 1, BENCH_MARKET: 0}
    orphans = {BENCH_RANDOM: 0, BENCH_MARKET: ORPHAN_TOLERANCE_ROWS + 1}

    status, exit_code, roto, sospechoso = compute_verdict(
        agg=agg, drift_paired=drift_effective, drift_effective=drift_effective,
        gaps=_ZERO, orphans=orphans, leaks=[], pending_max_age_h=None,
    )
    assert status == "ROTO"
    assert exit_code == 2
    assert any("effective drift" in r for r in roto)
    assert any("orphan" in r for r in sospechoso)
