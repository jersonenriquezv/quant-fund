"""Engine 1 meta-label scorer — in-process scoring with the frozen model.

Loads `models/engine1_meta_v1.pkl` (frozen 2026-06-23) and scores a live
engine1 `TradeSetup`'s feature dict with the SAME transform the offline
analysis uses, so the live score reproduces the offline score exactly.

Design (parity is the whole point — see Phase 1 of
docs/plans/engine1-ml-filter-live.md):
- Reuse `scripts.ml_v0_engine1.prepare_features` for the column drop + dtype
  coercion. One transform path → no drift between offline and live.
- Force the frozen model's categorical CATEGORIES onto the row so single-row
  inference can never assign different category codes than training did.
- Reindex to the frozen `feature_names` so column order matches the booster.

The cutoff (`settings.ENGINE1_SCORE_CUTOFF`) is a FROZEN rank threshold, not a
calibrated probability — `passes_cutoff` is a simple `score >= cutoff`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Mapping

import pandas as pd

from config.settings import settings

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "engine1_meta_v1.pkl"


@lru_cache(maxsize=1)
def _load_model() -> dict:
    """Load + cache the frozen model artifact. Raises if missing/corrupt."""
    import joblib

    # Trusted source: this artifact is produced locally by
    # scripts/ml_v1_freeze_model.py and committed to our own repo — not a
    # third-party download. joblib/pickle load is safe here.
    artifact = joblib.load(MODEL_PATH)
    required = {"model", "feature_names", "cat_cols", "cat_categories"}
    missing = required - set(artifact)
    if missing:
        raise ValueError(f"engine1 model artifact missing keys: {missing}")
    return artifact


def _to_frame(features) -> pd.DataFrame:
    """Normalize dict / Series / DataFrame to a DataFrame for prepare_features."""
    if isinstance(features, pd.DataFrame):
        return features.copy()
    if isinstance(features, pd.Series):
        return features.to_frame().T
    if isinstance(features, Mapping):
        return pd.DataFrame([dict(features)])
    raise TypeError(f"unsupported feature container: {type(features)}")


def score_features(features) -> float:
    """Return P(tp) in [0,1] for one engine1 setup's feature dict.

    `features` is the same dict `shared.ml_features.extract_setup_features`
    produces at detection (what gets inserted into ml_setups). Extra keys are
    ignored; missing model features become NaN (LightGBM handles natively).
    """
    return float(score_many(features)[0])


def score_many(features) -> list[float]:
    """Vectorized form of score_features over a DataFrame / list of rows."""
    # Local import keeps the scripts/ dependency off module-load of the live bot.
    from scripts.ml_v0_engine1 import prepare_features

    artifact = _load_model()
    model = artifact["model"]
    feat_names = artifact["feature_names"]
    cat_cols = artifact["cat_cols"]
    cat_categories = artifact["cat_categories"]

    df = _to_frame(features)
    # prepare_features builds the label from outcome_type; live rows have none.
    # A dummy keeps the transform identical without affecting the dropped X.
    if "outcome_type" not in df.columns:
        df["outcome_type"] = "shadow_sl"

    X, _, _ = prepare_features(df)
    X = X.reindex(columns=feat_names)
    # Force the training-time categories so single-row inference cannot drift.
    for col in cat_cols:
        if col in X.columns:
            X[col] = pd.Categorical(X[col], categories=cat_categories[col])

    proba = model.predict_proba(X)[:, 1]
    return [float(p) for p in proba]


def passes_cutoff(score: float) -> bool:
    """True if the setup is in the live-eligible top tercile (score >= cutoff)."""
    return score >= settings.ENGINE1_SCORE_CUTOFF
