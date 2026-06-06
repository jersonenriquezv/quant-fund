#!/usr/bin/env python3
"""ML v0 — engine1_trend_pullback meta-label baseline.

Issue: https://github.com/jersonenriquezv/quant-fund/issues/25

Goal: answer one binary question — do the features captured in `ml_setups`
contain information predictive of engine1 outcomes, or is the 24% WR vs
21.7% bench_random gap noise?

Approach:
- Pull engine1_trend_pullback rows with feature_version >= 4 and a clean
  binary outcome (shadow_tp or shadow_sl). Exclude shadow_breakeven and
  shadow_timeout — ambiguous for binary classification.
- Drop identity columns (would leak), outcome columns (would be the label),
  and timestamps (high cardinality, not generalizable in this small N).
- LightGBM binary classifier, class_weight balanced, fixed seed, early
  stopping. Hyperparameters chosen to avoid overfitting at small N
  (num_leaves=15, min_data_in_leaf=5).
- Time-based 80/20 split sorted by created_at — no look-ahead. Random split
  would inflate AUC since later setups can leak via correlated market state.
- Report AUC train/test, top-15 feature importance, decision verdict per
  the rules in the linked issue.

Why no walk-forward purged k-fold: N=~58 makes any k-fold variance huge.
Single holdout is appropriate for v0 — purpose is "is there signal at all",
not "production-ready model". When N reaches 200+, switch to purged k-fold.

Run:
    python scripts/ml_v0_engine1.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import psycopg2
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402

SETUP_TYPE = "engine1_trend_pullback"
MIN_FEATURE_VERSION = 4
RANDOM_STATE = 42
HOLDOUT_FRAC = 0.20
# Date-stamped per run so re-runs never overwrite a prior report (the 2026-05-25
# baseline must survive for comparison). Override with ML_V0_REPORT_PATH if needed.
REPORT_PATH = Path(
    os.environ.get(
        "ML_V0_REPORT_PATH",
        Path(__file__).resolve().parent.parent
        / "docs" / "audits"
        / f"ml-v0-engine1-{datetime.utcnow():%Y-%m-%d}.md",
    )
)

# Columns to drop before training. Three categories:
# 1. Identity / metadata — would leak setup_type membership or be high-cardinality nonsense.
# 2. Outcome — these ARE the label or proxies for it.
# 3. Timestamps — Unix ms or datetimes; high cardinality, not generalizable in this N.
DROP_COLUMNS = {
    # Identity / metadata. `pair` and `direction` stay — they are legitimate
    # features (BTC and ETH have different microstructure; longs and shorts
    # behave differently in trending markets).
    "id", "setup_id", "feature_version", "setup_type", "experiment_id",
    # Absolute price levels — high-cardinality per-pair proxies. With BTC
    # ~$60k and DOGE ~$0.15, the model learns the pair through these instead
    # of through the explicit `pair` feature. Their percentage / ratio
    # counterparts (risk_distance_pct, rr_ratio, entry_distance_pct,
    # sl_distance_pct) carry the same info pair-invariantly.
    "entry_price", "sl_price", "tp1_price", "tp2_price",
    "current_price_at_detection",
    # Outcome / label-derived (would be the label itself or a deterministic proxy)
    "outcome_type", "pnl_pct", "pnl_usd", "actual_entry", "actual_exit",
    "exit_reason", "fill_duration_ms", "trade_duration_ms", "resolved_at",
    "guardian_close_reason",
    # Post-fill / post-resolution leakage — captures the candle that FIRED
    # the outcome, which is by definition a perfect TP/SL predictor.
    "shadow_fill_time_ms", "shadow_fill_candle_ts", "shadow_fill_candle_tf",
    "shadow_fill_candle_volume_ratio",
    "shadow_resolve_candle_close", "shadow_resolve_candle_high",
    "shadow_resolve_candle_low", "shadow_resolve_candle_tf",
    "shadow_resolve_candle_ts",
    # Slippage + depth at entry — computed when shadow monitor records the
    # synthetic fill. Even though they describe pre-fill state, they only
    # exist for filled rows; their NULL pattern correlates with outcome.
    "shadow_slippage_estimate_pct", "shadow_depth_at_entry",
    # Mid-trade guardian flags — set after detection during monitoring; leaks
    # information about how the trade unfolded.
    "guardian_shadow_counter", "guardian_shadow_cvd",
    "guardian_shadow_momentum", "guardian_shadow_stall",
    # Risk service post-decision — what we want to predict instead.
    "risk_reject_reason", "risk_approved",
    # Shadow path flags — describe pipeline state, not market state.
    "shadow_mode",
    # Timestamps — high cardinality, not generalizable at N=58.
    "timestamp", "created_at",
}


def fetch_data() -> pd.DataFrame:
    """Pull engine1 rows with binary outcomes from PostgreSQL.

    Returns a DataFrame sorted by created_at so the time split is deterministic.
    """
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
          AND feature_version >= %s
          AND outcome_type IN ('shadow_tp', 'shadow_sl')
        ORDER BY created_at
    """
    df = pd.read_sql(query, conn, params=(SETUP_TYPE, MIN_FEATURE_VERSION))
    conn.close()
    return df


def prepare_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """Drop identity/outcome/timestamp cols, build label, return (X, y, cat_cols).

    Per-column policy:
    - Booleans → int8 (LightGBM splits 0/1 either way; explicit cast keeps the
      dtype check happy).
    - Numeric `object` dtypes (mixed None/float from PostgreSQL) → coerced to
      float with NaN for missing. LightGBM handles NaN natively as a missing
      value split.
    - Pure-string `object` dtypes → pandas `category`, passed to LightGBM as
      native categorical so it learns optimal splits without one-hot.
    """
    y = (df["outcome_type"] == "shadow_tp").astype(int)
    X = df.drop(columns=[c for c in DROP_COLUMNS if c in df.columns])

    cat_cols: list[str] = []
    for col in X.columns:
        s = X[col]
        if s.dtype == bool:
            X[col] = s.astype("Int8")
            continue
        if pd.api.types.is_numeric_dtype(s):
            continue
        # Object dtype — disambiguate numeric-with-None vs true string.
        coerced = pd.to_numeric(s, errors="coerce")
        non_null = s.notna().sum()
        if non_null == 0 or coerced.notna().sum() / max(non_null, 1) >= 0.9:
            # Mostly numeric (>=90% coerce-able). Treat as numeric with NaN.
            X[col] = coerced
        else:
            X[col] = s.astype("category")
            cat_cols.append(col)

    return X, y, cat_cols


def split_time(X: pd.DataFrame, y: pd.Series, holdout_frac: float) -> tuple:
    """Sorted-by-time 80/20 holdout split. df comes in sorted by created_at."""
    n_test = max(1, int(len(X) * holdout_frac))
    n_train = len(X) - n_test
    X_train = X.iloc[:n_train].copy()
    X_test = X.iloc[n_train:].copy()
    y_train = y.iloc[:n_train].copy()
    y_test = y.iloc[n_train:].copy()
    return X_train, X_test, y_train, y_test


def train(X_train, y_train, X_test, y_test, cat_cols) -> lgb.Booster:
    """Train LightGBM with early stopping on the holdout."""
    train_set = lgb.Dataset(X_train, label=y_train, categorical_feature=cat_cols)
    test_set = lgb.Dataset(X_test, label=y_test, categorical_feature=cat_cols, reference=train_set)
    pos = float(y_train.sum())
    neg = float(len(y_train) - pos)
    scale_pos_weight = neg / pos if pos > 0 else 1.0
    params = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 5,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "scale_pos_weight": scale_pos_weight,
        "seed": RANDOM_STATE,
        "verbose": -1,
    }
    model = lgb.train(
        params,
        train_set,
        num_boost_round=200,
        valid_sets=[train_set, test_set],
        valid_names=["train", "test"],
        callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=False), lgb.log_evaluation(0)],
    )
    return model


def evaluate(model: lgb.Booster, X_train, y_train, X_test, y_test) -> dict:
    """Return AUC train + AUC test plus the gain-based feature importance series."""
    train_preds = model.predict(X_train, num_iteration=model.best_iteration)
    test_preds = model.predict(X_test, num_iteration=model.best_iteration)
    auc_train = roc_auc_score(y_train, train_preds)
    auc_test = roc_auc_score(y_test, test_preds)
    importance = pd.Series(
        model.feature_importance(importance_type="gain", iteration=model.best_iteration),
        index=model.feature_name(),
    ).sort_values(ascending=False)
    return {"auc_train": auc_train, "auc_test": auc_test, "importance": importance}


def verdict(auc_test: float, auc_train: float, n_test: int) -> tuple[str, str, str]:
    """Map AUC to verdict + recommended action per issue #25 decision rules.

    Returns (verdict_label, action, confidence_note). The confidence note flags
    cases where the headline AUC should be treated with extra skepticism:
    - Overfit gap (train - test > 0.20) means the model is memorizing training
      noise; the test AUC at small N may be lucky rather than skill.
    - Small holdout (n_test < 20) makes AUC variance wide; estimate +/- 0.15.
    """
    if auc_test > 0.60:
        label = "EDGE CLARO"
        action = "Continuar recolectando engine1, iterar modelo, no construir Engine 2 todavía."
    elif auc_test >= 0.55:
        label = "SEÑAL DÉBIL"
        action = "Recolectar 4 semanas más (target N=300), re-train v1, decidir."
    elif auc_test >= 0.50:
        label = "MARGINAL"
        action = "Construir Engine 2 (Failed Breakout) según strategy_redesign_2026_04.md §4.2."
    else:
        label = "ANTI-EDGE"
        action = "Audit profundo — features posiblemente anti-correlacionadas con outcomes."
    notes = []
    if (auc_train - auc_test) > 0.20:
        notes.append(
            f"Overfit gap (train={auc_train:.2f}, test={auc_test:.2f}) > 0.20. "
            "Model memorizes training set; test AUC at small N is unreliable."
        )
    if n_test < 20:
        notes.append(
            f"Holdout N={n_test} < 20. Standard error on test AUC is wide "
            "(~+/-0.15). Verdict above is provisional until N grows."
        )
    confidence = " ".join(notes) if notes else "Train/test gap and N look healthy."
    return label, action, confidence


def write_report(df, X, y, X_train, X_test, y_train, y_test, metrics: dict) -> Path:
    """Markdown report. Lives in backtest_results/ alongside backtest TRACKER."""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    v_label, v_action, v_confidence = verdict(
        metrics["auc_test"], metrics["auc_train"], len(X_test)
    )
    lines = []
    lines.append(f"# ML v0 — engine1_trend_pullback meta-label baseline")
    lines.append("")
    lines.append(f"_Generated: {datetime.utcnow().isoformat()}Z. Issue: #25._")
    lines.append("")
    lines.append("## Dataset")
    lines.append("")
    lines.append(f"- **Setup type:** `{SETUP_TYPE}`")
    lines.append(f"- **Feature version filter:** `feature_version >= {MIN_FEATURE_VERSION}`")
    lines.append(f"- **Outcome filter:** `shadow_tp` (label=1) or `shadow_sl` (label=0)")
    lines.append(f"- **N total:** {len(df)}")
    lines.append(f"- **Class balance:** {int(y.sum())} TP / {int(len(y) - y.sum())} SL ({y.mean() * 100:.1f}% positive)")
    lines.append(f"- **Date range:** {df['created_at'].min()} → {df['created_at'].max()}")
    lines.append(f"- **Experiments included:** {', '.join(sorted(df['experiment_id'].dropna().unique()))}")
    lines.append(f"- **Feature columns:** {X.shape[1]} (after dropping identity/outcome/timestamp)")
    lines.append("")
    lines.append("## Split")
    lines.append("")
    lines.append(f"- **Strategy:** time-sorted 80/20 holdout (no look-ahead)")
    lines.append(f"- **Train N:** {len(X_train)} ({int(y_train.sum())} TP / {int(len(y_train) - y_train.sum())} SL)")
    lines.append(f"- **Test N:** {len(X_test)} ({int(y_test.sum())} TP / {int(len(y_test) - y_test.sum())} SL)")
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append(f"- **AUC train:** {metrics['auc_train']:.4f}")
    lines.append(f"- **AUC test:**  {metrics['auc_test']:.4f}")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**{v_label}** — {v_action}")
    lines.append("")
    lines.append(f"_Confidence: {v_confidence}_")
    lines.append("")
    lines.append("| AUC test | Veredicto | Acción |")
    lines.append("|---|---|---|")
    lines.append("| > 0.60 | Edge claro | Continuar engine1, no construir Engine 2 |")
    lines.append("| 0.55–0.60 | Señal débil | Recolectar más, re-train |")
    lines.append("| 0.50–0.55 | Marginal | Construir Engine 2 |")
    lines.append("| < 0.50 | Anti-edge | Audit |")
    lines.append("")
    lines.append("## Top-15 Feature Importance (gain)")
    lines.append("")
    lines.append("| Rank | Feature | Importance |")
    lines.append("|---|---|---|")
    for i, (feat, imp) in enumerate(metrics["importance"].head(15).items(), 1):
        lines.append(f"| {i} | `{feat}` | {imp:.2f} |")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append("- N is small; AUC variance is wide. Re-train at N=200, N=300 to confirm trend.")
    lines.append("- Multiple `experiment_id` regimes mixed — parameter shifts across rows may add noise.")
    lines.append("- `shadow_breakeven` and `shadow_timeout` excluded. These contain useful info for")
    lines.append("  multi-class classification later but are ambiguous for v0 binary.")
    lines.append("- No hyperparameter tuning. Defaults chosen to be conservative against overfitting.")
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines))
    return REPORT_PATH


def main() -> int:
    print(f"ml_v0_engine1 starting — setup={SETUP_TYPE}, fv>={MIN_FEATURE_VERSION}")
    df = fetch_data()
    if len(df) < 20:
        print(f"ERROR: only {len(df)} rows — need >= 20 to train. Wait for more shadow data.")
        return 1
    X, y, cat_cols = prepare_features(df)
    X_train, X_test, y_train, y_test = split_time(X, y, HOLDOUT_FRAC)
    if y_train.nunique() < 2 or y_test.nunique() < 2:
        print("ERROR: one class missing in train or test split. Cannot compute AUC.")
        return 1
    model = train(X_train, y_train, X_test, y_test, cat_cols)
    metrics = evaluate(model, X_train, y_train, X_test, y_test)
    path = write_report(df, X, y, X_train, X_test, y_train, y_test, metrics)
    v_label, _, v_confidence = verdict(
        metrics["auc_test"], metrics["auc_train"], len(X_test)
    )
    print()
    print(f"  N total: {len(df)}  (train {len(X_train)} / test {len(X_test)})")
    print(f"  AUC train: {metrics['auc_train']:.4f}")
    print(f"  AUC test:  {metrics['auc_test']:.4f}")
    print(f"  Verdict:   {v_label}")
    print(f"  Confidence: {v_confidence}")
    print(f"  Report:    {path}")
    print()
    print("Top-10 features:")
    for feat, imp in metrics["importance"].head(10).items():
        print(f"  {feat:<35} {imp:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
