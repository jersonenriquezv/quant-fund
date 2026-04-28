#!/usr/bin/env python3
"""Engine 1 shadow health report — read-only.

Summarizes the live state of Engine 1 + benchmarks for the active experiment:
  - counts per setup_type x outcome_type
  - resolved n + TP/SL/BE breakdown
  - dedup rate
  - pending (NULL outcome) rows + stale-pending age
  - pair leakage outside expected scope (BTC/ETH)
  - co-emission drift between engine1_trend_pullback and its two benchmarks
  - sample-starved warning if Engine 1 resolved < N_TARGET

Usage:
    python scripts/report_engine1_shadow.py

Exit codes:
    0 — sano or sample-starved
    1 — sospechoso (dedup rate high, BE rate high, mild drift)
    2 — roto (pair leakage, co-emission drift > tolerance, stale pending > 24h)

Read-only: never writes to DB, never sends Telegram alerts.
"""
from __future__ import annotations

import sys
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
EXPECTED_PAIRS: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
N_TARGET = 50
PENDING_STALE_HOURS = 24
DRIFT_TOLERANCE_ROWS = 2
DEDUP_RATE_WARN = 0.70

RESOLVED_OUTCOMES = ("shadow_tp", "shadow_sl", "shadow_breakeven")


def _db():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=5,
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


def _fmt_int(n: int | None) -> str:
    return f"{n:>4}" if n is not None else "  - "


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def fetch_counts(cur) -> dict[tuple[str, str, str], int]:
    cur.execute(
        """
        SELECT setup_type, pair, COALESCE(outcome_type, '__pending__') AS outcome, COUNT(*)
        FROM ml_setups
        WHERE experiment_id = %s
          AND setup_type = ANY(%s)
        GROUP BY setup_type, pair, outcome
        ORDER BY setup_type, pair, outcome
        """,
        (settings.EXPERIMENT_ID, list(ENGINE1_SETUPS)),
    )
    return {(r[0], r[1], r[2]): int(r[3]) for r in cur.fetchall()}


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


def aggregate(counts: dict[tuple[str, str, str], int]) -> dict[str, dict[str, int]]:
    """Per-setup totals: total, resolved, tp, sl, be, dedup, pair_filtered, pending,
    in-scope-emissions (excl. pair_filtered)."""
    agg: dict[str, dict[str, int]] = {}
    for setup in ENGINE1_SETUPS:
        agg[setup] = {
            "total": 0, "resolved": 0, "tp": 0, "sl": 0, "be": 0,
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


def coemission_drift(agg: dict[str, dict[str, int]]) -> dict[str, int]:
    """Drift between Engine 1 emissions and each benchmark."""
    e1 = agg[ENGINE1_PRIMARY]["in_scope_emissions"]
    return {
        s: agg[s]["in_scope_emissions"] - e1
        for s in ENGINE1_SETUPS if s != ENGINE1_PRIMARY
    }


def main() -> int:
    conn = _db()
    try:
        with conn.cursor() as cur:
            counts = fetch_counts(cur)
            pending_n, pending_max_age_h = fetch_pending_age(cur)
    finally:
        conn.close()

    agg = aggregate(counts)
    leaks = pair_leakage(counts)
    drift = coemission_drift(agg)

    print("=" * 72)
    print("ENGINE 1 SHADOW REPORT")
    print("=" * 72)
    print(f"experiment_id  : {settings.EXPERIMENT_ID}")
    print(f"feature_version: {settings.ML_FEATURE_VERSION}")
    print(f"expected_pairs : {', '.join(EXPECTED_PAIRS)}")
    print(f"target n       : {N_TARGET} resolved for Engine 1")

    section("Per-setup totals")
    print(f"{'setup_type':<34} {'total':>6} {'emis':>5} {'res':>4} {'tp':>4} {'sl':>4} {'be':>4} {'dedup':>6} {'pndg':>5} {'pf':>5}")
    for s in ENGINE1_SETUPS:
        a = agg[s]
        print(
            f"{s:<34} {a['total']:>6} {a['in_scope_emissions']:>5} {a['resolved']:>4} "
            f"{a['tp']:>4} {a['sl']:>4} {a['be']:>4} {a['dedup']:>6} {a['pending']:>5} {a['pair_filtered']:>5}"
        )
    print("  emis = in-scope emissions (BTC+ETH, excl. pair_filtered)")
    print("  pf   = shadow_pair_filtered (quarantine - working as intended)")

    section("Dedup rates (within in-scope emissions)")
    for s in ENGINE1_SETUPS:
        a = agg[s]
        emis = a["in_scope_emissions"]
        rate = a["dedup"] / emis if emis else 0.0
        flag = "  WARN HIGH" if rate > DEDUP_RATE_WARN else ""
        print(f"  {s:<34} {a['dedup']:>4}/{emis:<4} = {rate*100:5.1f}%{flag}")

    section("Co-emission drift vs Engine 1")
    e1_emis = agg[ENGINE1_PRIMARY]["in_scope_emissions"]
    print(f"  {ENGINE1_PRIMARY} emissions: {e1_emis}")
    for s, delta in drift.items():
        flag = "  WARN DRIFT" if abs(delta) > DRIFT_TOLERANCE_ROWS else ""
        print(f"  {s:<34} delta = {delta:+d}{flag}")

    section("Pair leakage (rows outside BTC/ETH not pair_filtered)")
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
    e1_resolved = agg[ENGINE1_PRIMARY]["resolved"]
    sample_starved = e1_resolved < N_TARGET

    roto_reasons: list[str] = []
    sospechoso_reasons: list[str] = []

    if leaks:
        roto_reasons.append(f"pair leakage: {len(leaks)} row group(s) outside BTC/ETH")
    for s, delta in drift.items():
        if abs(delta) > DRIFT_TOLERANCE_ROWS:
            roto_reasons.append(f"co-emission drift {s}: {delta:+d} (tol ±{DRIFT_TOLERANCE_ROWS})")
    if pending_max_age_h is not None and pending_max_age_h > PENDING_STALE_HOURS:
        roto_reasons.append(f"stale pending: {pending_max_age_h:.1f}h > {PENDING_STALE_HOURS}h")

    for s in ENGINE1_SETUPS:
        a = agg[s]
        emis = a["in_scope_emissions"]
        if emis and a["dedup"] / emis > DEDUP_RATE_WARN:
            sospechoso_reasons.append(f"dedup rate {s}: {a['dedup']/emis*100:.1f}%")

    if roto_reasons:
        print("  status: ROTO")
        for r in roto_reasons:
            print(f"    - {r}")
        return 2
    if sospechoso_reasons:
        print("  status: SOSPECHOSO")
        for r in sospechoso_reasons:
            print(f"    - {r}")
        if sample_starved:
            print(f"    - sample-starved: Engine 1 resolved {e1_resolved}/{N_TARGET}")
        return 1
    if sample_starved:
        print(f"  status: SAMPLE-STARVED - Engine 1 resolved {e1_resolved}/{N_TARGET}")
        print("  pipeline healthy, more time needed before edge eval")
        return 0
    print(f"  status: SANO - Engine 1 resolved {e1_resolved} >= {N_TARGET}")
    print("  ready for edge comparison vs benchmarks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
