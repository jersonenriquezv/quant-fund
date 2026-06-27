#!/usr/bin/env python3
"""Phase 1 gate — prove the in-process engine1 scorer reproduces the offline score.

The live bot will score each engine1 setup ONE ROW AT A TIME via
strategy_service.engines.engine1_scorer. The offline analysis scores the whole
batch at once. If single-row scoring diverges from batch scoring (e.g. dtype /
categorical inference changing with batch size), the live filter is wrong.

This script scores the most recent N resolved engine1 rows both ways and reports
the max absolute difference. Gate: every row matches within TOL.

Run:  python scripts/engine1_scorer_parity.py
Exit: 0 if all rows within TOL (PASS), 1 otherwise (FAIL).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from scripts.ml_v0_engine1 import prepare_features  # noqa: E402
from strategy_service.engines import engine1_scorer  # noqa: E402

TOL = 1e-3
N_ROWS = 40


def _batch_scores(df: pd.DataFrame) -> list[float]:
    """Offline reference: transform + score the whole batch with the frozen model."""
    art = engine1_scorer._load_model()
    X, _, _ = prepare_features(df.copy())
    X = X.reindex(columns=art["feature_names"])
    for col in art["cat_cols"]:
        if col in X.columns:
            X[col] = pd.Categorical(X[col], categories=art["cat_categories"][col])
    return [float(p) for p in art["model"].predict_proba(X)[:, 1]]


def main() -> int:
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=10,
    )
    conn.set_session(readonly=True, autocommit=True)
    query = """
        SELECT * FROM ml_setups
        WHERE setup_type = 'engine1_trend_pullback'
          AND feature_version >= 4
          AND outcome_type IN ('shadow_tp', 'shadow_sl')
          AND (data_quality IS NULL OR data_quality <> 'partial_candle_risk')
        ORDER BY created_at DESC
        LIMIT %s
    """
    df = pd.read_sql(query, conn, params=(N_ROWS,))
    conn.close()

    n = len(df)
    if n < 10:
        print(f"FAIL — only {n} rows, need >= 10 for a meaningful parity check")
        return 1

    batch = _batch_scores(df)
    # Live path: score each row independently as a dict, like main.py will.
    per_row = [engine1_scorer.score_features(df.iloc[[i]].to_dict("records")[0])
               for i in range(n)]

    diffs = [abs(a - b) for a, b in zip(batch, per_row)]
    max_diff = max(diffs)
    n_fail = sum(d > TOL for d in diffs)

    cutoff = settings.ENGINE1_SCORE_CUTOFF
    n_pass_cutoff = sum(s >= cutoff for s in per_row)
    print(f"engine1 scorer parity — N={n} recent engine1 rows")
    print(f"  max |batch - per_row| = {max_diff:.2e}  (tol {TOL:.0e})")
    print(f"  rows over tol: {n_fail}")
    print(f"  score range: {min(per_row):.3f}..{max(per_row):.3f}  "
          f"cutoff {cutoff} -> {n_pass_cutoff}/{n} eligible (~top tercile)")

    if n_fail == 0:
        print(f"  PASS — in-process score reproduces offline within {TOL:.0e}")
        return 0
    print("  FAIL — per-row scoring diverges from batch; do NOT wire live")
    # Show worst offenders for debugging.
    worst = sorted(range(n), key=lambda i: -diffs[i])[:5]
    for i in worst:
        print(f"    row {i}: batch={batch[i]:.4f} per_row={per_row[i]:.4f} diff={diffs[i]:.2e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
