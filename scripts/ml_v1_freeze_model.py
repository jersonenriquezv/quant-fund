#!/usr/bin/env python3
"""Freeze the engine1 meta-label model for forward validation.

Trains LGBMClassifier on ALL current engine1 binary rows + sample weights and
saves it with the metadata the forward scorer needs (feature columns, cat cols,
and the cutoff = newest training created_at). Everything with created_at AFTER
the cutoff is then a genuine forward, unseen-at-train-time test sample.

Re-run only to RE-freeze (resets the forward window). Normal flow: freeze once,
then let scripts/ml_v1_forward_check.py accumulate.

Run:  python scripts/ml_v1_freeze_model.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import joblib
import lightgbm as lgb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.feature_importance import compute_sample_uniqueness  # noqa: E402
from scripts.ml_v0_engine1 import prepare_features  # noqa: E402
from scripts.ml_v1_meta_label import fetch_for, LGB_PARAMS  # noqa: E402

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "engine1_meta_v1.pkl"


def main() -> int:
    df = fetch_for("engine1_trend_pullback")
    if len(df) < 40:
        print(f"ERROR: {len(df)} rows, need >= 40 to freeze.")
        return 1

    start_t = pd.to_datetime(df["created_at"]).reset_index(drop=True)
    end_t = pd.to_datetime(df["resolved_at"]).reset_index(drop=True).fillna(start_t)
    X, y, cat = prepare_features(df)
    X = X.reset_index(drop=True); y = y.reset_index(drop=True)
    w = compute_sample_uniqueness(start_t, end_t).reset_index(drop=True)

    clf = lgb.LGBMClassifier(**LGB_PARAMS)
    clf.fit(X, y, sample_weight=w.values, categorical_feature=cat or "auto")

    cutoff = str(df["created_at"].max())
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": clf,
        "feature_names": list(X.columns),
        "cat_cols": cat,
        "cutoff_created_at": cutoff,
        "train_n": len(df),
        "setup_type": "engine1_trend_pullback",
    }, MODEL_PATH)

    print(f"frozen: {MODEL_PATH}")
    print(f"  trained on N={len(df)} engine1 trades")
    print(f"  cutoff_created_at = {cutoff}")
    print(f"  forward window = any engine1 trade created AFTER that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
