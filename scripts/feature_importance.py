"""
Feature importance analysis for ML setup data.

Implements AFML Ch.8 feature importance methods:
- MDI (Mean Decrease Impurity) — fast, biased toward high-cardinality features
- MDA (Mean Decrease Accuracy) — slower, unbiased, uses purged k-fold CV
- SFI (Single Feature Importance) — per-feature OOS score

Uses AFML Ch.7 purged k-fold cross-validation:
- Purges training samples whose outcomes overlap with test period
- Adds embargo period after each test fold to prevent leakage

Usage:
    python scripts/feature_importance.py                    # defaults
    python scripts/feature_importance.py --min-version 5    # only v5+ data
    python scripts/feature_importance.py --label quality    # trade quality model
    python scripts/feature_importance.py --top 15           # show top 15
"""

import argparse
import sys
import os
import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, log_loss
from sklearn.model_selection import BaseCrossValidator

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

warnings.filterwarnings("ignore", category=FutureWarning)


# ================================================================
# Purged K-Fold CV (AFML Ch.7)
# ================================================================

class PurgedKFoldCV(BaseCrossValidator):
    """K-fold cross-validation with purging and embargo.

    Per AFML Ch.7:
    - Purge: remove training samples whose holding period overlaps with
      any test sample's holding period.
    - Embargo: after each test fold boundary, skip an additional fraction
      of samples to prevent information leakage from lagged features.

    Args:
        n_splits: Number of folds.
        embargo_pct: Fraction of total samples to embargo after test boundary.
    """

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.01):
        self.n_splits = n_splits
        self.embargo_pct = embargo_pct

    def get_n_splits(self, X=None, y=None, groups=None):
        return self.n_splits

    def split(self, X, y=None, groups=None):
        """Yield (train_idx, test_idx) with purging and embargo.

        X must have a DatetimeIndex or be indexed by observation time.
        groups: pd.Series of holding-period end times (same index as X).
            If None, no purging is applied (falls back to standard k-fold).
        """
        n = len(X)
        indices = np.arange(n)
        embargo_size = int(n * self.embargo_pct)
        fold_size = n // self.n_splits

        for i in range(self.n_splits):
            test_start = i * fold_size
            test_end = min((i + 1) * fold_size, n)
            test_idx = indices[test_start:test_end]

            # Start with all non-test indices
            train_idx = np.concatenate([indices[:test_start], indices[test_end:]])

            # Apply embargo: remove samples right after test end
            if embargo_size > 0 and test_end < n:
                embargo_end = min(test_end + embargo_size, n)
                embargo_mask = (train_idx >= test_end) & (train_idx < embargo_end)
                train_idx = train_idx[~embargo_mask]

            # Apply purging: remove training samples whose holding period
            # overlaps with the test period
            if groups is not None:
                test_times = X.index[test_idx]
                test_start_time = test_times.min()
                test_end_time = test_times.max()

                # groups[i] = end time of holding period for sample i
                train_mask = np.ones(len(train_idx), dtype=bool)
                for j, tidx in enumerate(train_idx):
                    hold_end = groups.iloc[tidx]
                    obs_time = X.index[tidx]
                    # Purge if holding period overlaps test window
                    if obs_time < test_end_time and hold_end > test_start_time:
                        train_mask[j] = False

                train_idx = train_idx[train_mask]

            yield train_idx, test_idx


# ================================================================
# Feature importance methods (AFML Ch.8)
# ================================================================

def mdi_importance(clf, feature_names: list[str]) -> pd.Series:
    """Mean Decrease Impurity (AFML Ch.8.3).

    Fast but biased toward high-cardinality / continuous features.
    """
    importances = clf.feature_importances_
    return pd.Series(importances, index=feature_names).sort_values(ascending=False)


def mda_importance(
    clf, X: pd.DataFrame, y: pd.Series,
    cv: PurgedKFoldCV, groups: pd.Series | None = None,
) -> pd.Series:
    """Mean Decrease Accuracy (AFML Ch.8.4).

    Permutes each feature and measures OOS accuracy drop.
    Unbiased but slower. Uses purged k-fold CV.
    """
    scores = {}
    baseline_scores = []

    for train_idx, test_idx in cv.split(X, y, groups):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
            continue

        clf_fold = clf.__class__(**clf.get_params())
        clf_fold.fit(X_train, y_train)
        baseline = accuracy_score(y_test, clf_fold.predict(X_test))
        baseline_scores.append(baseline)

        for col in X.columns:
            X_perm = X_test.copy()
            X_perm[col] = np.random.permutation(X_perm[col].values)
            perm_score = accuracy_score(y_test, clf_fold.predict(X_perm))
            drop = baseline - perm_score
            scores.setdefault(col, []).append(drop)

    result = {col: np.mean(drops) for col, drops in scores.items()}
    return pd.Series(result).sort_values(ascending=False)


def sfi_importance(
    clf, X: pd.DataFrame, y: pd.Series,
    cv: PurgedKFoldCV, groups: pd.Series | None = None,
) -> pd.Series:
    """Single Feature Importance (AFML Ch.8.5).

    Trains a separate model per feature. Shows standalone predictive power.
    """
    scores = {}

    for col in X.columns:
        fold_scores = []
        for train_idx, test_idx in cv.split(X, y, groups):
            X_train = X.iloc[train_idx][[col]]
            X_test = X.iloc[test_idx][[col]]
            y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

            if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
                continue

            clf_single = clf.__class__(
                n_estimators=50, max_depth=3, random_state=42, n_jobs=1
            )
            clf_single.fit(X_train, y_train)
            fold_scores.append(accuracy_score(y_test, clf_single.predict(X_test)))

        if fold_scores:
            scores[col] = np.mean(fold_scores)

    return pd.Series(scores).sort_values(ascending=False)


# ================================================================
# Data loading
# ================================================================

def load_ml_data(min_version: int = 4) -> pd.DataFrame:
    """Load ml_setups data from PostgreSQL."""
    import psycopg2

    # Load DB config
    from config.settings import settings

    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )

    query = f"""
        SELECT * FROM ml_setups
        WHERE feature_version >= {min_version}
        ORDER BY created_at
    """
    df = pd.read_sql(query, conn)
    conn.close()

    print(f"Loaded {len(df)} rows (feature_version >= {min_version})")
    if "outcome_type" in df.columns:
        outcome_dist = df["outcome_type"].value_counts()
        print(f"\nOutcome distribution:\n{outcome_dist.to_string()}")

    return df


def prepare_features(
    df: pd.DataFrame,
    label_mode: str = "fill",
) -> tuple[pd.DataFrame, pd.Series, pd.Series | None]:
    """Prepare feature matrix X, labels y, and optional holding-period end times.

    Args:
        df: Raw ml_setups DataFrame.
        label_mode: "fill" for fill-probability, "quality" for trade-quality.

    Returns:
        (X, y, groups) where groups is end-of-holding-period for purging.
    """
    if label_mode == "fill":
        # Fill probability: did the setup get filled?
        filled_types = {"filled_tp", "filled_sl", "filled_trailing", "filled_timeout"}
        df = df[df["outcome_type"].notna()].copy()
        df["label"] = df["outcome_type"].isin(filled_types).astype(int)
    elif label_mode == "quality":
        # Trade quality: profitable fill or not?
        filled_types = {"filled_tp", "filled_sl", "filled_trailing", "filled_timeout"}
        df = df[df["outcome_type"].isin(filled_types)].copy()
        if "pnl_pct" not in df.columns or df["pnl_pct"].isna().all():
            print("ERROR: No pnl_pct data for quality model")
            sys.exit(1)
        df["label"] = (df["pnl_pct"] > 0).astype(int)
    else:
        raise ValueError(f"Unknown label_mode: {label_mode}")

    print(f"\nLabel distribution ({label_mode}):")
    print(df["label"].value_counts().to_string())
    print(f"Total samples: {len(df)}")

    # Select numeric/boolean feature columns (exclude outcomes, metadata, IDs)
    exclude_cols = {
        "id", "setup_id", "feature_version", "timestamp",
        "outcome_type", "pnl_pct", "pnl_usd", "actual_entry", "actual_exit",
        "exit_reason", "fill_duration_ms", "trade_duration_ms",
        "guardian_close_reason", "created_at", "resolved_at", "label",
        # Absolute prices — leak temporal info (AFML Ch.7)
        "entry_price", "sl_price", "tp1_price", "tp2_price",
        "current_price_at_detection",
        # Risk context — potentially leaky for quality model
    }

    if label_mode == "quality":
        # Exclude risk context for quality model (leakage risk)
        exclude_cols.update({
            "risk_capital", "risk_open_positions",
            "risk_daily_dd_pct", "risk_weekly_dd_pct", "risk_trades_today",
        })

    # Encode categoricals
    cat_cols = {"pair", "direction", "setup_type", "htf_bias", "ob_timeframe",
                "pd_zone", "sweep_tier", "funding_tier", "oi_rising_tier",
                "dominance_tier"}

    feature_cols = []
    for col in df.columns:
        if col in exclude_cols:
            continue
        if col in cat_cols:
            feature_cols.append(col)
        elif df[col].dtype in ("float64", "int64", "bool", "float32", "int32"):
            feature_cols.append(col)

    X = df[feature_cols].copy()

    # One-hot encode categoricals
    for col in cat_cols:
        if col in X.columns:
            dummies = pd.get_dummies(X[col], prefix=col, dummy_na=True)
            X = pd.concat([X.drop(columns=[col]), dummies], axis=1)

    # Fill NaN with -1 (flag for missing, distinguishable from 0)
    X = X.fillna(-1)

    # Convert booleans to int
    for col in X.columns:
        if X[col].dtype == "bool":
            X[col] = X[col].astype(int)

    y = df["label"]

    # Holding period end times for purging
    groups = None
    if "resolved_at" in df.columns and "created_at" in df.columns:
        df_times = df[["created_at", "resolved_at"]].copy()
        df_times["created_at"] = pd.to_datetime(df_times["created_at"])
        df_times["resolved_at"] = pd.to_datetime(df_times["resolved_at"])
        # Fill missing resolved_at with created_at + 12h (max duration)
        df_times["resolved_at"] = df_times["resolved_at"].fillna(
            df_times["created_at"] + pd.Timedelta(hours=12)
        )
        X.index = df_times["created_at"].values
        y.index = X.index
        groups = df_times["resolved_at"]
        groups.index = X.index

    return X, y, groups


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="ML Feature Importance Analysis (AFML Ch.8)")
    parser.add_argument("--min-version", type=int, default=4, help="Minimum feature version")
    parser.add_argument("--label", choices=["fill", "quality"], default="fill",
                        help="Label mode: fill-probability or trade-quality")
    parser.add_argument("--top", type=int, default=20, help="Number of top features to show")
    parser.add_argument("--n-folds", type=int, default=5, help="CV folds")
    parser.add_argument("--embargo", type=float, default=0.02, help="Embargo fraction")
    parser.add_argument("--skip-mda", action="store_true", help="Skip MDA (slow)")
    parser.add_argument("--skip-sfi", action="store_true", help="Skip SFI (slow)")
    args = parser.parse_args()

    print("=" * 60)
    print("ML FEATURE IMPORTANCE — AFML Ch.8")
    print("=" * 60)

    # Load data
    df = load_ml_data(min_version=args.min_version)
    if len(df) < 10:
        print(f"\nERROR: Only {len(df)} samples — need at least 10 for analysis.")
        print("Collect more data before running feature importance.")
        sys.exit(1)

    # Prepare features
    X, y, groups = prepare_features(df, label_mode=args.label)
    print(f"\nFeature matrix: {X.shape[0]} samples × {X.shape[1]} features")

    if len(y.unique()) < 2:
        print("\nERROR: Only one class present — cannot train classifier.")
        sys.exit(1)

    # Classifier
    clf = RandomForestClassifier(
        n_estimators=200,
        max_depth=5,
        min_samples_leaf=3,
        random_state=42,
        n_jobs=-1,
    )

    # Purged k-fold CV
    cv = PurgedKFoldCV(n_splits=args.n_folds, embargo_pct=args.embargo)

    # --- MDI ---
    print("\n" + "=" * 60)
    print("MDI — Mean Decrease Impurity (AFML Ch.8.3)")
    print("=" * 60)
    clf.fit(X, y)
    mdi = mdi_importance(clf, list(X.columns))
    print(f"\nTop {args.top} features (MDI):")
    for i, (feat, score) in enumerate(mdi.head(args.top).items()):
        bar = "#" * int(score * 200)
        print(f"  {i+1:2d}. {feat:<40s} {score:.4f}  {bar}")

    # --- MDA ---
    if not args.skip_mda:
        print("\n" + "=" * 60)
        print("MDA — Mean Decrease Accuracy (AFML Ch.8.4)")
        print("  Uses purged k-fold CV with embargo")
        print("=" * 60)
        mda = mda_importance(clf, X, y, cv, groups)
        print(f"\nTop {args.top} features (MDA):")
        for i, (feat, score) in enumerate(mda.head(args.top).items()):
            bar = "#" * max(0, int(score * 500))
            print(f"  {i+1:2d}. {feat:<40s} {score:+.4f}  {bar}")

    # --- SFI ---
    if not args.skip_sfi:
        print("\n" + "=" * 60)
        print("SFI — Single Feature Importance (AFML Ch.8.5)")
        print("=" * 60)
        sfi = sfi_importance(clf, X, y, cv, groups)
        print(f"\nTop {args.top} features (SFI — standalone accuracy):")
        baseline_acc = y.value_counts().max() / len(y)
        print(f"  Baseline (majority class): {baseline_acc:.4f}")
        for i, (feat, score) in enumerate(sfi.head(args.top).items()):
            delta = score - baseline_acc
            marker = "+" if delta > 0 else " "
            print(f"  {i+1:2d}. {feat:<40s} {score:.4f}  ({marker}{delta:.4f} vs baseline)")

    # --- Cross-validated score ---
    print("\n" + "=" * 60)
    print("CROSS-VALIDATED SCORE (purged k-fold)")
    print("=" * 60)
    cv_scores = []
    for train_idx, test_idx in cv.split(X, y, groups):
        X_tr, X_te = X.iloc[train_idx], X.iloc[test_idx]
        y_tr, y_te = y.iloc[train_idx], y.iloc[test_idx]
        if len(np.unique(y_tr)) < 2:
            continue
        clf_cv = RandomForestClassifier(
            n_estimators=200, max_depth=5, min_samples_leaf=3,
            random_state=42, n_jobs=-1,
        )
        clf_cv.fit(X_tr, y_tr)
        cv_scores.append(accuracy_score(y_te, clf_cv.predict(X_te)))

    if cv_scores:
        baseline_acc = y.value_counts().max() / len(y)
        mean_acc = np.mean(cv_scores)
        std_acc = np.std(cv_scores)
        print(f"  Baseline accuracy: {baseline_acc:.4f}")
        print(f"  CV accuracy:       {mean_acc:.4f} ± {std_acc:.4f}")
        print(f"  Lift over baseline: {mean_acc - baseline_acc:+.4f}")
        if mean_acc <= baseline_acc:
            print("  WARNING: Model does not beat majority-class baseline.")
            print("  Features may not be predictive, or sample size is too small.")
    else:
        print("  Could not compute CV scores (insufficient data per fold)")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
