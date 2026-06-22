#!/usr/bin/env python3
"""ML v1 — honest meta-label evaluation (purged CV + sample weights).

Successor to scripts/ml_v0_engine1.py. Same data + label, but replaces the
naive time-sorted 80/20 holdout with the AFML-correct validation the v0 script
itself promised "when N reaches 200+" (engine1 is now N=320):

  1. PURGED k-fold CV (AFML Ch.7) — purges training samples whose holding
     period overlaps a test fold, plus embargo. Kills the leakage that lets a
     single holdout report an inflated AUC.
  2. SAMPLE UNIQUENESS WEIGHTS (AFML Ch.4) — engine1's clustered-impulse history
     means overlapping labels share information; uniqueness down-weights them so
     the AUC reflects independent evidence, not repeated near-duplicate bets.
  3. CALIBRATION (Brier score) — the predicted probability must be trustworthy
     before it can size bets (Kelly). Reported so we know if v1 is deploy-ready.

The headline output is the HONEST cross-validated AUC next to v0's naive-holdout
AUC, so we can see how much of the "edge claro 0.78" was an overfit mirage.

Reuses the validated primitives already in the repo:
  - PurgedKFoldCV, compute_sample_uniqueness  (scripts/feature_importance.py)
  - DROP_COLUMNS, prepare_features, fetch_data (scripts/ml_v0_engine1.py)

Run:
    python scripts/ml_v1_meta_label.py            # engine1_trend_pullback
    SETUP_TYPE=scalp_liq_reclaim_v1 python scripts/ml_v1_meta_label.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score, brier_score_loss

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from scripts.feature_importance import PurgedKFoldCV, compute_sample_uniqueness  # noqa: E402
from scripts.ml_v0_engine1 import (  # noqa: E402
    DROP_COLUMNS, prepare_features, fetch_data, split_time, train as train_holdout,
)

SETUP_TYPE = os.environ.get("SETUP_TYPE", "engine1_trend_pullback")
N_SPLITS = int(os.environ.get("ML_V1_SPLITS", "5"))
EMBARGO_PCT = float(os.environ.get("ML_V1_EMBARGO", "0.02"))
RANDOM_STATE = 42

# LightGBM sklearn params — mirror ml_v0 conservative settings so the ONLY
# difference vs the v0 baseline is the validation method, not the model.
LGB_PARAMS = dict(
    objective="binary",
    n_estimators=200,
    learning_rate=0.05,
    num_leaves=15,
    min_child_samples=5,
    subsample=0.9,
    colsample_bytree=0.9,
    subsample_freq=1,
    random_state=RANDOM_STATE,
    verbose=-1,
)


def fetch_for(setup_type: str) -> pd.DataFrame:
    """Pull binary-outcome rows for a setup, keeping created_at + resolved_at
    (needed for purge groups + uniqueness) which prepare_features later drops."""
    import psycopg2
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=10,
    )
    conn.set_session(readonly=True, autocommit=True)
    query = """
        SELECT *
        FROM ml_setups
        WHERE setup_type = %s
          AND feature_version >= 4
          AND outcome_type IN ('shadow_tp', 'shadow_sl')
          AND (data_quality IS NULL OR data_quality <> 'partial_candle_risk')
        ORDER BY created_at
    """
    df = pd.read_sql(query, conn, params=(setup_type,))
    conn.close()
    return df


def purged_cv_auc(X, y, groups, sample_weight, cat_cols) -> dict:
    """Cross-validated AUC under PurgedKFoldCV with sample-uniqueness weights.

    Pools out-of-fold predictions to compute one AUC over all test folds (more
    stable at this N than averaging per-fold AUCs, some of which are tiny)."""
    cv = PurgedKFoldCV(n_splits=N_SPLITS, embargo_pct=EMBARGO_PCT)
    oof_pred = np.full(len(X), np.nan)
    fold_aucs, purged_counts = [], []

    for train_idx, test_idx in cv.split(X, y, groups):
        if len(train_idx) == 0 or len(test_idx) == 0:
            continue
        y_tr = y.iloc[train_idx]
        if y_tr.nunique() < 2:
            continue
        clf = lgb.LGBMClassifier(**LGB_PARAMS)
        clf.fit(
            X.iloc[train_idx], y_tr,
            sample_weight=sample_weight.iloc[train_idx].values,
            categorical_feature=cat_cols or "auto",
        )
        p = clf.predict_proba(X.iloc[test_idx])[:, 1]
        oof_pred[test_idx] = p
        y_te = y.iloc[test_idx]
        if y_te.nunique() == 2:
            fold_aucs.append(roc_auc_score(y_te, p, sample_weight=sample_weight.iloc[test_idx].values))
        # purged count = how many train rows were removed vs naive (n - test - train)
        purged_counts.append(len(X) - len(test_idx) - len(train_idx))

    mask = ~np.isnan(oof_pred)
    pooled_auc = roc_auc_score(y[mask], oof_pred[mask], sample_weight=sample_weight[mask].values) \
        if y[mask].nunique() == 2 else float("nan")
    brier = brier_score_loss(y[mask], oof_pred[mask], sample_weight=sample_weight[mask].values)
    return {
        "pooled_auc": pooled_auc,
        "fold_aucs": fold_aucs,
        "mean_fold_auc": float(np.mean(fold_aucs)) if fold_aucs else float("nan"),
        "std_fold_auc": float(np.std(fold_aucs)) if fold_aucs else float("nan"),
        "brier": brier,
        "n_scored": int(mask.sum()),
        "avg_purged_per_fold": float(np.mean(purged_counts)) if purged_counts else 0.0,
    }


def main() -> int:
    print(f"ml_v1_meta_label — setup={SETUP_TYPE}, purged {N_SPLITS}-fold, embargo {EMBARGO_PCT:.0%}")
    df = fetch_for(SETUP_TYPE)
    if len(df) < 40:
        print(f"ERROR: only {len(df)} binary rows — need >= 40 for {N_SPLITS}-fold CV.")
        return 1

    # Capture time columns BEFORE prepare_features drops them.
    start_t = pd.to_datetime(df["created_at"]).reset_index(drop=True)
    end_t = pd.to_datetime(df["resolved_at"]).reset_index(drop=True)
    # Fallback: if resolved_at missing, use created_at (zero-length holding = no purge effect).
    end_t = end_t.fillna(start_t)

    X, y, cat_cols = prepare_features(df)
    X = X.reset_index(drop=True)
    y = y.reset_index(drop=True)

    # Sample uniqueness (down-weights overlapping/clustered labels).
    weights = compute_sample_uniqueness(start_t, end_t)
    weights = weights.reset_index(drop=True)

    # groups = holding-period END time; X indexed by observation (start) time.
    X.index = start_t.values
    groups = pd.Series(end_t.values, index=X.index)

    # --- Honest: purged CV + uniqueness weights
    cvres = purged_cv_auc(X, y, groups, weights, cat_cols)

    # --- Reference: reproduce v0's naive holdout on the SAME rows (no weights, no purge)
    Xb, yb, catb = prepare_features(df)
    Xtr, Xte, ytr, yte = split_time(Xb, yb, 0.20)
    naive_model = train_holdout(Xtr, ytr, Xte, yte, catb)
    naive_auc = roc_auc_score(yte, naive_model.predict(Xte, num_iteration=naive_model.best_iteration))

    pos, neg = int(y.sum()), int(len(y) - y.sum())
    print()
    print(f"  N binary:          {len(df)}  ({pos} TP / {neg} SL, {y.mean()*100:.1f}% positive)")
    print(f"  Mean uniqueness:   {weights.mean():.3f}  (1.0 = fully independent; lower = more overlap)")
    print(f"  Avg purged/fold:   {cvres['avg_purged_per_fold']:.0f} train rows removed by purge+embargo")
    print()
    print(f"  AUC naive holdout (v0 method):  {naive_auc:.3f}   <- inflated reference")
    print(f"  AUC purged CV pooled (HONEST):  {cvres['pooled_auc']:.3f}")
    print(f"  AUC purged CV per-fold mean:    {cvres['mean_fold_auc']:.3f} +/- {cvres['std_fold_auc']:.3f}")
    print(f"  Brier score (calibration):      {cvres['brier']:.3f}   (0=perfect, 0.25=coin flip)")
    print()
    drop = naive_auc - cvres["pooled_auc"]
    honest = cvres["pooled_auc"]
    if honest >= 0.60:
        verdict = "EDGE HOLDS — honest AUC still >= 0.60. Proceed to calibration + bet-sizing."
    elif honest >= 0.55:
        verdict = "WEAK — honest AUC 0.55-0.60. Edge real but thin; do NOT size on it yet."
    elif honest >= 0.50:
        verdict = "MIRAGE — honest AUC ~coin flip. The v0 edge was mostly overfit."
    else:
        verdict = "ANTI-EDGE — honest AUC < 0.50. Features anti-correlated; stop."
    print(f"  Overfit drop (naive - honest):  {drop:+.3f}")
    print(f"  VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
