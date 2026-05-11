#!/usr/bin/env python3
"""Engine 1 shadow health report — read-only.

Summarizes the live state of Engine 1 + benchmarks for the active experiment:
  - counts per setup_type x outcome_type (raw pipeline health)
  - resolved n + TP/SL/BE/timeout breakdown
  - dedup rate
  - pending (NULL outcome) rows + stale-pending age
  - pair leakage outside expected scope (defaults to SHADOW_PAIR_FILTER
    entry for Engine 1; falls back to all TRADING_PAIRS when omitted, which
    is the v1c "all pairs short-only" mode)
  - co-emission drift between engine1_trend_pullback and its two benchmarks,
    computed over a paired cohort defined by detection-event proximity
  - orphan benchmark rows (in-scope bench rows from a detection pass with
    no in-scope Engine 1 row)
  - sample-starved warning if Engine 1 resolved < N_TARGET

Pairing semantics (detection-pass clustering):
  `setup.timestamp` is anchored to a structural feature (impulse origin,
  OB candle, swing) so it can repeat across many detection passes — it is
  NOT a trigger identity. Each pipeline pass instead inserts rows into
  ml_setups within a sub-second window: engine1 row first, then
  bench_random, then bench_market. We cluster rows by (pair, created_at)
  proximity within `PAIRING_WINDOW_SECONDS` and treat each cluster as one
  detection pass. A bench row is paired iff its cluster contains at least
  one in-scope (outcome != shadow_pair_filtered) Engine 1 row, and orphan
  otherwise. This catches the failure mode where Engine 1 is suppressed
  by the (pair, direction, setup_type) pipeline dedup but its benchmarks
  emit because their dedup history differs.

  Future clean fix: emit benchmarks with an explicit `origin_signal_id`
  pointing at the engine1 setup_id. That removes the time-window heuristic.
  Not done yet — first solving for current data without changing emission.

Usage:
    python scripts/report_engine1_shadow.py

Exit codes:
    0 — sano or sample-starved
    1 — sospechoso (dedup rate high, mild orphan drift)
    2 — roto (pair leakage, paired drift > tolerance, stale pending > 24h,
            orphan rows > explode threshold)

Read-only: never writes to DB, never sends Telegram alerts.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from config.settings import settings


ENGINE1_SETUPS: tuple[str, ...] = (
    "engine1_trend_pullback",
    "bench_engine1_random_direction",
    "bench_engine1_market_now",
)
ENGINE1_PRIMARY = "engine1_trend_pullback"
EXPECTED_PAIRS: tuple[str, ...] = tuple(
    settings.SHADOW_PAIR_FILTER.get(ENGINE1_PRIMARY, settings.TRADING_PAIRS)
)
N_TARGET = 50
PENDING_STALE_HOURS = 24
DRIFT_TOLERANCE_ROWS = 2
ORPHAN_TOLERANCE_ROWS = 2
ORPHAN_EXPLODE_ROWS = 20
# Passes where Engine 1 emitted in-scope but a benchmark did not. Caused
# by `_SHADOW_DEDUP_TTL_SECONDS = 300` exactly matching 5m candle cadence:
# real-world processing jitter occasionally pushes the bench's elapsed
# time below 300s and pipeline dedup blocks the row before _ml_log_setup.
# bench_market_now is the most exposed because its (pair, direction,
# setup_type) key never varies; bench_random_direction often dodges via
# the sha256 flip changing direction. These gaps are not Engine 1 logic
# failures — treat as observability and drive SOSPECHOSO when they exceed
# tolerance, never ROTO.
JITTER_GAP_TOLERANCE_ROWS = 2
DEDUP_RATE_WARN = 0.70
# Maximum gap between consecutive same-pair ml_setups inserts that still
# count as the same detection pass. Real-world spread between engine1 and
# its benchmarks is sub-second; 2s gives slack for DB / GIL jitter.
PAIRING_WINDOW_SECONDS = 2.0

RESOLVED_OUTCOMES = ("shadow_tp", "shadow_sl", "shadow_breakeven", "shadow_timeout")

# (setup_type, pair, timestamp_ms, outcome_or_pending, created_at)
Row = tuple[str, str, int, str, datetime]


def _db():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=5,
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def fetch_rows(cur) -> list[Row]:
    """Per-row pull of Engine 1 + benchmark ml_setups for the active experiment."""
    cur.execute(
        """
        SELECT setup_type, pair, timestamp,
               COALESCE(outcome_type, '__pending__') AS outcome,
               created_at
        FROM ml_setups
        WHERE experiment_id = %s
          AND setup_type = ANY(%s)
        """,
        (settings.EXPERIMENT_ID, list(ENGINE1_SETUPS)),
    )
    return [(r[0], r[1], int(r[2]), r[3], r[4]) for r in cur.fetchall()]


def fetch_pending_age(cur) -> tuple[int, float | None]:
    cur.execute(
        """
        SELECT COUNT(*),
               MAX(EXTRACT(EPOCH FROM (NOW() - to_timestamp(timestamp/1000))) / 3600.0)
        FROM ml_setups
        WHERE experiment_id = %s
          AND setup_type = ANY(%s)
          AND outcome_type IS NULL
        """,
        (settings.EXPERIMENT_ID, list(ENGINE1_SETUPS)),
    )
    row = cur.fetchone()
    return int(row[0] or 0), float(row[1]) if row[1] is not None else None


def counts_from_rows(rows: list[Row]) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    for st, pair, _ts, outcome, _ca in rows:
        key = (st, pair, outcome)
        counts[key] = counts.get(key, 0) + 1
    return counts


def aggregate(counts: dict[tuple[str, str, str], int]) -> dict[str, dict[str, int]]:
    """Per-setup totals: total, resolved, tp, sl, be, to, dedup, pair_filtered, pending,
    in-scope-emissions (excl. pair_filtered).

    `resolved` = tp + sl + be + to (terminal outcomes, timeout counted separately).
    """
    agg: dict[str, dict[str, int]] = {}
    for setup in ENGINE1_SETUPS:
        agg[setup] = {
            "total": 0, "resolved": 0, "tp": 0, "sl": 0, "be": 0, "to": 0,
            "dedup": 0, "pair_filtered": 0, "pending": 0,
            "in_scope_emissions": 0,
        }
    for (setup, _pair, outcome), n in counts.items():
        if setup not in agg:
            continue
        agg[setup]["total"] += n
        if outcome == "shadow_tp":
            agg[setup]["tp"] += n
            agg[setup]["resolved"] += n
            agg[setup]["in_scope_emissions"] += n
        elif outcome == "shadow_sl":
            agg[setup]["sl"] += n
            agg[setup]["resolved"] += n
            agg[setup]["in_scope_emissions"] += n
        elif outcome == "shadow_breakeven":
            agg[setup]["be"] += n
            agg[setup]["resolved"] += n
            agg[setup]["in_scope_emissions"] += n
        elif outcome == "shadow_timeout":
            agg[setup]["to"] += n
            agg[setup]["resolved"] += n
            agg[setup]["in_scope_emissions"] += n
        elif outcome == "shadow_dedup":
            agg[setup]["dedup"] += n
            agg[setup]["in_scope_emissions"] += n
        elif outcome == "shadow_pair_filtered":
            agg[setup]["pair_filtered"] += n
        elif outcome == "__pending__":
            agg[setup]["pending"] += n
            agg[setup]["in_scope_emissions"] += n
        # any other outcome (shadow_no_fill, shadow_orphaned, etc.)
        # counts toward total only — handled implicitly.
    return agg


def pair_leakage(counts: dict[tuple[str, str, str], int]) -> list[tuple[str, str, str, int]]:
    """Rows in unexpected pairs that are NOT shadow_pair_filtered (real leakage)."""
    leaks: list[tuple[str, str, str, int]] = []
    for (setup, pair, outcome), n in counts.items():
        if pair in EXPECTED_PAIRS:
            continue
        if outcome == "shadow_pair_filtered":
            continue  # quarantine working as intended
        leaks.append((setup, pair, outcome, n))
    return leaks


def cluster_passes(
    rows: list[Row], *, window_seconds: float = PAIRING_WINDOW_SECONDS,
) -> list[list[Row]]:
    """Group rows into detection passes by (pair, created_at proximity).

    Rows on the same pair whose `created_at` is within `window_seconds`
    of the previous row in chronological order form one cluster. Each
    cluster represents one pipeline detection pass — engine1 + its
    benchmarks all insert within sub-second windows. Returns a flat
    list of clusters; cluster ordering is not meaningful.
    """
    by_pair: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_pair[r[1]].append(r)
    passes: list[list[Row]] = []
    for pair_rows in by_pair.values():
        sorted_rows = sorted(pair_rows, key=lambda r: r[4])
        cur_pass: list[Row] = []
        cur_t: datetime | None = None
        for r in sorted_rows:
            ca = r[4]
            if cur_t is None or (ca - cur_t).total_seconds() <= window_seconds:
                cur_pass.append(r)
            else:
                passes.append(cur_pass)
                cur_pass = [r]
            cur_t = ca
        if cur_pass:
            passes.append(cur_pass)
    return passes


def classify_bench(
    rows: list[Row], passes: list[list[Row]],
) -> dict[str, dict[str, list[Row]]]:
    """Partition in-scope bench rows into paired vs orphan via cluster membership.

    A bench row is in-scope iff pair in EXPECTED_PAIRS and outcome !=
    shadow_pair_filtered (pair-filtered rows are quarantine artefacts,
    not orphans). Paired = the bench row's detection pass contains at
    least one in-scope Engine 1 row. Orphan = the bench row sits in a
    pass with no in-scope Engine 1 row — caused by direction-flipping
    benchmarks dodging the pipeline (pair, direction, setup_type) dedup
    key when Engine 1 itself was pipeline-deduped.
    """
    result: dict[str, dict[str, list[Row]]] = {
        s: {"paired": [], "orphan": []}
        for s in ENGINE1_SETUPS if s != ENGINE1_PRIMARY
    }
    for cluster in passes:
        has_engine1_in_scope = any(
            r[0] == ENGINE1_PRIMARY and r[3] != "shadow_pair_filtered"
            for r in cluster
        )
        for st, pair, ts, outcome, ca in cluster:
            if st == ENGINE1_PRIMARY:
                continue
            if st not in result:
                continue
            if pair not in EXPECTED_PAIRS:
                continue
            if outcome == "shadow_pair_filtered":
                continue
            bucket = "paired" if has_engine1_in_scope else "orphan"
            result[st][bucket].append((st, pair, ts, outcome, ca))
    return result


def aggregate_paired(
    classification: dict[str, dict[str, list[Row]]],
) -> dict[str, dict[str, int]]:
    """Per-bench paired-only aggregate, mirroring the aggregate() schema."""
    paired_agg: dict[str, dict[str, int]] = {}
    for bench, parts in classification.items():
        counts: dict[tuple[str, str, str], int] = {}
        for st, pair, _ts, outcome, _ca in parts["paired"]:
            counts[(st, pair, outcome)] = counts.get((st, pair, outcome), 0) + 1
        paired_agg[bench] = aggregate(counts)[bench]
    return paired_agg


def coemission_drift_paired(
    engine1_emis: int, paired_agg: dict[str, dict[str, int]],
) -> dict[str, int]:
    """Drift = paired bench in-scope emissions - Engine 1 in-scope emissions.

    Negative drift on a bench is partly explained by `jitter_gaps` (passes
    where engine1 emitted in-scope but the bench was blocked by pipeline
    dedup at the 300s TTL boundary). For a verdict that excludes that
    explained component see `coemission_drift_effective`.
    """
    return {
        s: paired_agg[s]["in_scope_emissions"] - engine1_emis
        for s in paired_agg
    }


def jitter_gaps(passes: list[list[Row]]) -> dict[str, int]:
    """Per-bench passes where Engine 1 was in-scope but the bench did not emit.

    Pipeline dedup TTL = 300s exactly matches the 5m candle cadence; sub-second
    processing jitter occasionally pushes the bench's `(now - last_eval)` just
    below 300s and the row never reaches `_ml_log_setup`. Counted here so the
    verdict can exclude the explained component from drift before flagging
    ROTO. Pair-filtered engine1 rows are not "in-scope" sources, so passes
    where the only engine1 row is shadow_pair_filtered do not count as gaps.
    """
    gaps: dict[str, int] = {
        s: 0 for s in ENGINE1_SETUPS if s != ENGINE1_PRIMARY
    }
    for cluster in passes:
        has_engine1_in_scope = any(
            r[0] == ENGINE1_PRIMARY and r[3] != "shadow_pair_filtered"
            for r in cluster
        )
        if not has_engine1_in_scope:
            continue
        cluster_pair = cluster[0][1]
        if cluster_pair not in EXPECTED_PAIRS:
            continue
        for bench in gaps:
            has_bench_in_scope = any(
                r[0] == bench
                and r[1] in EXPECTED_PAIRS
                and r[3] != "shadow_pair_filtered"
                for r in cluster
            )
            if not has_bench_in_scope:
                gaps[bench] += 1
    return gaps


def coemission_drift_effective(
    engine1_emis: int,
    paired_agg: dict[str, dict[str, int]],
    gaps: dict[str, int],
) -> dict[str, int]:
    """Drift after subtracting TTL-jitter explained gaps from the engine1 baseline.

    `effective = paired_bench - (engine1_emis - jitter_gaps[bench])`.
    When the only reason a bench underemits is TTL-boundary jitter, this is 0.
    """
    return {
        s: paired_agg[s]["in_scope_emissions"] - (engine1_emis - gaps.get(s, 0))
        for s in paired_agg
    }


def orphan_counts(
    classification: dict[str, dict[str, list[Row]]],
) -> dict[str, int]:
    return {s: len(p["orphan"]) for s, p in classification.items()}


def compute_verdict(
    *,
    agg: dict[str, dict[str, int]],
    drift_paired: dict[str, int],
    drift_effective: dict[str, int],
    gaps: dict[str, int],
    orphans: dict[str, int],
    leaks: list[tuple[str, str, str, int]],
    pending_max_age_h: float | None,
) -> tuple[str, int, list[str], list[str]]:
    """Pure verdict logic — returns (status, exit_code, roto_reasons, sospechoso_reasons).

    status one of: "ROTO", "SOSPECHOSO", "SAMPLE-STARVED", "SANO".

    Edge-comparison drift is `drift_effective` (paired drift minus
    TTL-jitter gaps): only that drives ROTO. Raw `drift_paired` and
    `gaps` are surfaced as observability — large jitter gaps drive
    SOSPECHOSO, not ROTO, because they are pipeline dedup TTL boundary
    artefacts and not Engine 1 logic or schema failures. Mid-experiment
    we do not change `_SHADOW_DEDUP_TTL_SECONDS` to fix them, since that
    would alter sampling and require a new experiment_id.
    """
    e1_resolved = agg[ENGINE1_PRIMARY]["resolved"]
    sample_starved = e1_resolved < N_TARGET

    roto_reasons: list[str] = []
    sospechoso_reasons: list[str] = []

    if leaks:
        roto_reasons.append(
            f"pair leakage: {len(leaks)} row group(s) outside "
            f"{', '.join(EXPECTED_PAIRS)}"
        )
    for s, delta in drift_effective.items():
        if abs(delta) > DRIFT_TOLERANCE_ROWS:
            roto_reasons.append(
                f"effective drift {s}: {delta:+d} (tol +/-{DRIFT_TOLERANCE_ROWS}, "
                f"after excluding {gaps.get(s, 0)} TTL-jitter gap(s))"
            )
    for s, n in orphans.items():
        if n > ORPHAN_EXPLODE_ROWS:
            roto_reasons.append(
                f"orphan rows {s}: {n} (>{ORPHAN_EXPLODE_ROWS} = explode)"
            )
    if pending_max_age_h is not None and pending_max_age_h > PENDING_STALE_HOURS:
        roto_reasons.append(f"stale pending: {pending_max_age_h:.1f}h > {PENDING_STALE_HOURS}h")

    for s in ENGINE1_SETUPS:
        a = agg[s]
        emis = a["in_scope_emissions"]
        if emis and a["dedup"] / emis > DEDUP_RATE_WARN:
            sospechoso_reasons.append(f"dedup rate {s}: {a['dedup']/emis*100:.1f}%")
    for s, n in orphans.items():
        if ORPHAN_TOLERANCE_ROWS < n <= ORPHAN_EXPLODE_ROWS:
            sospechoso_reasons.append(
                f"orphan rows {s}: {n} (tol {ORPHAN_TOLERANCE_ROWS}, explode {ORPHAN_EXPLODE_ROWS})"
            )
    for s, n in gaps.items():
        if n > JITTER_GAP_TOLERANCE_ROWS:
            sospechoso_reasons.append(
                f"jitter gaps {s}: {n} (tol {JITTER_GAP_TOLERANCE_ROWS}, "
                f"TTL-boundary dedup, no engine1 logic issue)"
            )

    if roto_reasons:
        return "ROTO", 2, roto_reasons, sospechoso_reasons
    if sospechoso_reasons:
        return "SOSPECHOSO", 1, roto_reasons, sospechoso_reasons
    if sample_starved:
        return "SAMPLE-STARVED", 0, roto_reasons, sospechoso_reasons
    return "SANO", 0, roto_reasons, sospechoso_reasons


def main() -> int:
    conn = _db()
    try:
        with conn.cursor() as cur:
            rows = fetch_rows(cur)
            pending_n, pending_max_age_h = fetch_pending_age(cur)
    finally:
        conn.close()

    counts = counts_from_rows(rows)
    agg = aggregate(counts)
    leaks = pair_leakage(counts)
    passes = cluster_passes(rows)
    classification = classify_bench(rows, passes)
    paired_agg = aggregate_paired(classification)
    e1_emis = agg[ENGINE1_PRIMARY]["in_scope_emissions"]
    drift_paired = coemission_drift_paired(e1_emis, paired_agg)
    gaps = jitter_gaps(passes)
    drift_effective = coemission_drift_effective(e1_emis, paired_agg, gaps)
    orphans = orphan_counts(classification)

    n_passes_with_engine1 = sum(
        1 for cluster in passes
        if any(
            r[0] == ENGINE1_PRIMARY and r[3] != "shadow_pair_filtered"
            for r in cluster
        )
    )
    n_passes_orphan = sum(
        1 for cluster in passes
        if not any(r[0] == ENGINE1_PRIMARY and r[3] != "shadow_pair_filtered" for r in cluster)
        and any(
            r[0] != ENGINE1_PRIMARY and r[1] in EXPECTED_PAIRS
            and r[3] != "shadow_pair_filtered"
            for r in cluster
        )
    )

    print("=" * 72)
    print("ENGINE 1 SHADOW REPORT")
    print("=" * 72)
    print(f"experiment_id  : {settings.EXPERIMENT_ID}")
    print(f"feature_version: {settings.ML_FEATURE_VERSION}")
    print(f"expected_pairs : {', '.join(EXPECTED_PAIRS)}")
    print(f"target n       : {N_TARGET} resolved for Engine 1")
    print(f"pairing window : {PAIRING_WINDOW_SECONDS}s on (pair, created_at)")
    print(
        f"detection passes: {len(passes)} clusters "
        f"({n_passes_with_engine1} with engine1, {n_passes_orphan} bench-only)"
    )

    section("Per-setup totals (raw — pipeline health)")
    print(f"{'setup_type':<34} {'total':>6} {'emis':>5} {'res':>4} {'tp':>4} {'sl':>4} {'be':>4} {'to':>4} {'dedup':>6} {'pndg':>5} {'pf':>5}")
    for s in ENGINE1_SETUPS:
        a = agg[s]
        print(
            f"{s:<34} {a['total']:>6} {a['in_scope_emissions']:>5} {a['resolved']:>4} "
            f"{a['tp']:>4} {a['sl']:>4} {a['be']:>4} {a['to']:>4} {a['dedup']:>6} {a['pending']:>5} {a['pair_filtered']:>5}"
        )
    print(f"  emis = in-scope emissions ({', '.join(EXPECTED_PAIRS)}, excl. pair_filtered)")
    print("  res  = resolved = tp + sl + be + to (terminal outcomes)")
    print("  to   = shadow_timeout (terminal, separate from tp/sl/be)")
    print("  pf   = shadow_pair_filtered (quarantine - working as intended)")

    section("Per-bench paired-only totals (use for edge comparison)")
    print(f"{'setup_type':<34} {'emis':>5} {'res':>4} {'tp':>4} {'sl':>4} {'be':>4} {'to':>4} {'dedup':>6} {'pndg':>5}")
    for s in ENGINE1_SETUPS:
        if s == ENGINE1_PRIMARY:
            a = agg[s]  # Engine 1 trivially paired with itself — show raw in-scope
        else:
            a = paired_agg[s]
        print(
            f"{s:<34} {a['in_scope_emissions']:>5} {a['resolved']:>4} "
            f"{a['tp']:>4} {a['sl']:>4} {a['be']:>4} {a['to']:>4} {a['dedup']:>6} {a['pending']:>5}"
        )
    print(f"  paired = bench in detection pass (<= {PAIRING_WINDOW_SECONDS}s window) with engine1 row")

    section("Dedup rates (within in-scope emissions, raw)")
    for s in ENGINE1_SETUPS:
        a = agg[s]
        emis = a["in_scope_emissions"]
        rate = a["dedup"] / emis if emis else 0.0
        flag = "  WARN HIGH" if rate > DEDUP_RATE_WARN else ""
        print(f"  {s:<34} {a['dedup']:>4}/{emis:<4} = {rate*100:5.1f}%{flag}")

    section("Co-emission drift vs Engine 1 (paired only)")
    print(f"  {ENGINE1_PRIMARY} emissions: {e1_emis}")
    for s in drift_paired:
        delta_p = drift_paired[s]
        delta_e = drift_effective[s]
        gap = gaps.get(s, 0)
        flag = "  WARN DRIFT" if abs(delta_e) > DRIFT_TOLERANCE_ROWS else ""
        print(
            f"  {s:<34} paired = {delta_p:+d}  jitter_gaps = {gap}  "
            f"effective = {delta_e:+d}{flag}"
        )
    print("  effective = paired - (-jitter_gaps) = paired + jitter_gaps")
    print("  effective drives the verdict; paired/gaps are observability")

    section("Jitter gaps (engine1 in-scope, bench missing — TTL boundary artefact)")
    if not any(gaps.values()):
        print("  none — every in-scope engine1 pass has both benches in-scope")
    else:
        for s, n in gaps.items():
            if n == 0:
                continue
            flag = (
                f"  WARN (>{JITTER_GAP_TOLERANCE_ROWS})"
                if n > JITTER_GAP_TOLERANCE_ROWS else ""
            )
            print(f"  {s:<34} gaps = {n}{flag}")
        print("  cause: pipeline dedup TTL (300s) ~= 5m candle cadence;")
        print("    sub-second processing jitter occasionally pushes bench's")
        print("    `(now - last_eval)` below 300s and the row is blocked")
        print("    before _ml_log_setup. Not an Engine 1 logic failure.")
        print("    Do NOT change _SHADOW_DEDUP_TTL_SECONDS mid-experiment.")

    section("Orphan benchmark rows (bench in detection pass with no in-scope Engine 1 row)")
    if not any(orphans.values()):
        print("  none — every benchmark row sits in a pass with Engine 1")
    else:
        for s, n in orphans.items():
            if n == 0:
                continue
            if n > ORPHAN_EXPLODE_ROWS:
                flag = f"  WARN EXPLODE (>{ORPHAN_EXPLODE_ROWS})"
            elif n > ORPHAN_TOLERANCE_ROWS:
                flag = f"  WARN (>{ORPHAN_TOLERANCE_ROWS})"
            else:
                flag = ""
            print(f"  {s:<34} orphan = {n}{flag}")
        print("  cause: bench (pair, direction, setup_type) dedup key dodges")
        print("    Engine 1's pipeline dedup when bench direction flips, leaving")
        print("    a detection pass with bench rows but no engine1 row.")

    section(f"Pair leakage (rows outside {', '.join(EXPECTED_PAIRS)} not pair_filtered)")
    if not leaks:
        print("  none - quarantine intact")
    else:
        for setup, pair, outcome, n in leaks:
            print(f"  WARN {setup} {pair} {outcome} = {n}")

    section("Pending rows (NULL outcome_type)")
    print(f"  count: {pending_n}")
    if pending_max_age_h is not None:
        flag = "  WARN STALE" if pending_max_age_h > PENDING_STALE_HOURS else ""
        print(f"  oldest pending: {pending_max_age_h:.1f}h{flag}")

    # --- verdict ---
    section("Verdict")
    status, exit_code, roto_reasons, sospechoso_reasons = compute_verdict(
        agg=agg, drift_paired=drift_paired, drift_effective=drift_effective,
        gaps=gaps, orphans=orphans,
        leaks=leaks, pending_max_age_h=pending_max_age_h,
    )
    e1_resolved = agg[ENGINE1_PRIMARY]["resolved"]
    sample_starved = e1_resolved < N_TARGET

    if status == "ROTO":
        print("  status: ROTO")
        for r in roto_reasons:
            print(f"    - {r}")
    elif status == "SOSPECHOSO":
        print("  status: SOSPECHOSO")
        for r in sospechoso_reasons:
            print(f"    - {r}")
        if sample_starved:
            print(f"    - sample-starved: Engine 1 resolved {e1_resolved}/{N_TARGET}")
    elif status == "SAMPLE-STARVED":
        print(f"  status: SAMPLE-STARVED - Engine 1 resolved {e1_resolved}/{N_TARGET}")
        print("  pipeline healthy, more time needed before edge eval")
    else:
        print(f"  status: SANO - Engine 1 resolved {e1_resolved} >= {N_TARGET}")
        print("  ready for edge comparison vs benchmarks (paired cohort)")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
