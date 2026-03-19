"""
Feature importance analysis for ML setup data.

Implements AFML Ch.8 feature importance methods:
- MDI (Mean Decrease Impurity) — fast, biased toward high-cardinality features
- MDA (Mean Decrease Accuracy) — slower, unbiased, uses purged k-fold CV
- SFI (Single Feature Importance) — per-feature OOS score

Uses AFML Ch.7 purged k-fold cross-validation:
- Purges training samples whose outcomes overlap with test period
- Adds embargo period after each test fold to prevent leakage

AFML Ch.4 sample weights:
- Computes label uniqueness from indicator matrix (concurrent trades share info)
- Weights samples by inverse average concurrency

Usage:
    python scripts/feature_importance.py                    # defaults
    python scripts/feature_importance.py --min-version 5    # only v5+ data
    python scripts/feature_importance.py --label quality    # trade quality model
    python scripts/feature_importance.py --label barrier    # triple-barrier labels
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
# Purged K-Fold CV (AFML Ch.7, Snippets 7.1-7.3)
# ================================================================

class PurgedKFoldCV(BaseCrossValidator):
    """K-fold cross-validation with purging and embargo.

    Per AFML Ch.7:
    - Purge: remove training samples whose holding period overlaps with
      any test sample's holding period (three overlap cases).
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

            # Apply purging (AFML Snippet 7.1 — three overlap cases)
            if groups is not None:
                test_times = X.index[test_idx]
                test_start_time = test_times.min()
                test_end_time = test_times.max()

                train_mask = np.ones(len(train_idx), dtype=bool)
                for j, tidx in enumerate(train_idx):
                    hold_end = groups.iloc[tidx]
                    obs_time = X.index[tidx]
                    # Case 1: training obs started within test period
                    # Case 2: training obs ended within test period
                    # Case 3: training obs spans entire test period
                    if obs_time < test_end_time and hold_end > test_start_time:
                        train_mask[j] = False

                train_idx = train_idx[train_mask]

            yield train_idx, test_idx


# ================================================================
# Sample Weights (AFML Ch.4, Snippets 4.1-4.3)
# ================================================================

def compute_sample_uniqueness(
    start_times: pd.Series,
    end_times: pd.Series,
) -> pd.Series:
    """Compute average uniqueness per sample from indicator matrix.

    AFML Ch.4: when labels overlap in time, they share information.
    Uniqueness at bar t = 1/c_t where c_t = number of concurrent labels.
    Average uniqueness for label i = mean(1/c_t) over its holding period.

    Args:
        start_times: pd.Series of label start times (created_at).
        end_times: pd.Series of label end times (resolved_at).

    Returns:
        pd.Series of uniqueness weights (0 to 1) per sample.
    """
    n = len(start_times)
    if n == 0:
        return pd.Series(dtype=float)

    # Build concurrency count at each event boundary
    events = []
    for i in range(n):
        s = start_times.iloc[i]
        e = end_times.iloc[i]
        if pd.isna(s) or pd.isna(e):
            continue
        events.append((s, 1, i))   # label i starts
        events.append((e, -1, i))  # label i ends

    if not events:
        return pd.Series(1.0, index=start_times.index)

    events.sort(key=lambda x: (x[0], -x[1]))  # sort by time, ends before starts at same time

    # For each sample, track which time intervals it spans
    # and what concurrency exists at each interval
    uniqueness = pd.Series(1.0, index=start_times.index)

    # Compute concurrency for each sample efficiently
    for i in range(n):
        s = start_times.iloc[i]
        e = end_times.iloc[i]
        if pd.isna(s) or pd.isna(e):
            continue

        # Count how many other labels overlap with this one
        concurrent = 0
        count = 0
        for j in range(n):
            if i == j:
                continue
            sj = start_times.iloc[j]
            ej = end_times.iloc[j]
            if pd.isna(sj) or pd.isna(ej):
                continue
            # Check overlap
            if sj < e and ej > s:
                concurrent += 1

        # Average uniqueness = 1 / (1 + concurrent)
        uniqueness.iloc[i] = 1.0 / (1.0 + concurrent)

    # Normalize so weights sum to n
    total = uniqueness.sum()
    if total > 0:
        uniqueness = uniqueness * n / total

    return uniqueness


# ================================================================
# Feature importance methods (AFML Ch.8)
# ================================================================

def mdi_importance(clf, feature_names: list[str]) -> pd.DataFrame:
    """Mean Decrease Impurity (AFML Ch.8, Snippet 8.2).

    CRITICAL: clf must use max_features=1 so every feature gets evaluated.
    """
    df0 = {i: tree.feature_importances_ for i, tree in enumerate(clf.estimators_)}
    df0 = pd.DataFrame.from_dict(df0, orient="index")
    df0.columns = feature_names
    df0 = df0.replace(0, np.nan)  # Required when max_features=1
    imp = pd.concat({"mean": df0.mean(), "std": df0.std() * df0.shape[0] ** -0.5}, axis=1)
    imp /= imp["mean"].sum()
    return imp.sort_values("mean", ascending=False)


def mda_importance(
    clf, X: pd.DataFrame, y: pd.Series,
    cv: PurgedKFoldCV, groups: pd.Series | None = None,
    sample_weight: pd.Series | None = None,
) -> pd.DataFrame:
    """Mean Decrease Accuracy (AFML Ch.8, Snippet 8.3).

    Uses neg_log_loss scoring (AFML Ch.9 recommendation) with purged k-fold.
    """
    scr0 = pd.Series(dtype=float)
    scr1 = pd.DataFrame(columns=X.columns)

    for i, (train_idx, test_idx) in enumerate(cv.split(X, y, groups)):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
            continue

        w_train = sample_weight.iloc[train_idx].values if sample_weight is not None else None
        w_test = sample_weight.iloc[test_idx].values if sample_weight is not None else None

        clf_fold = clf.__class__(**clf.get_params())
        clf_fold.fit(X_train, y_train, sample_weight=w_train)

        # Baseline: neg_log_loss (AFML Ch.9 — penalizes confident wrong predictions)
        prob = clf_fold.predict_proba(X_test)
        scr0.loc[i] = -log_loss(y_test, prob, sample_weight=w_test, labels=clf_fold.classes_)

        for col in X.columns:
            X_perm = X_test.copy()
            np.random.shuffle(X_perm[col].values)
            prob_perm = clf_fold.predict_proba(X_perm)
            scr1.loc[i, col] = -log_loss(y_test, prob_perm, sample_weight=w_test, labels=clf_fold.classes_)

    if len(scr0) == 0:
        return pd.DataFrame(columns=["mean", "std"])

    # Importance = (baseline - permuted) / |permuted| (AFML Snippet 8.3)
    imp = (-scr1).add(scr0, axis=0)
    imp = imp / (-scr1)
    imp = pd.concat({"mean": imp.mean(), "std": imp.std() * imp.shape[0] ** -0.5}, axis=1)
    return imp.sort_values("mean", ascending=False)


def sfi_importance(
    clf, X: pd.DataFrame, y: pd.Series,
    cv: PurgedKFoldCV, groups: pd.Series | None = None,
    sample_weight: pd.Series | None = None,
) -> pd.DataFrame:
    """Single Feature Importance (AFML Ch.8, Snippet 8.4).

    Trains a model on EACH feature individually. No substitution effects.
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

            w_train = sample_weight.iloc[train_idx].values if sample_weight is not None else None

            clf_single = RandomForestClassifier(
                n_estimators=50, max_depth=3, random_state=42, n_jobs=1
            )
            clf_single.fit(X_train, y_train, sample_weight=w_train)
            fold_scores.append(accuracy_score(y_test, clf_single.predict(X_test)))

        if fold_scores:
            scores[col] = {"mean": np.mean(fold_scores), "std": np.std(fold_scores) * len(fold_scores) ** -0.5}

    result = pd.DataFrame(scores).T
    return result.sort_values("mean", ascending=False) if len(result) > 0 else pd.DataFrame(columns=["mean", "std"])


# ================================================================
# Data loading
# ================================================================

def load_ml_data(min_version: int = 4) -> pd.DataFrame:
    """Load ml_setups data from PostgreSQL."""
    import psycopg2

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
) -> tuple[pd.DataFrame, pd.Series, pd.Series | None, pd.Series | None]:
    """Prepare feature matrix X, labels y, holding-period end times, and sample weights.

    Args:
        df: Raw ml_setups DataFrame.
        label_mode: "fill", "quality", or "barrier" (AFML triple-barrier).

    Returns:
        (X, y, groups, sample_weight) where:
          groups = end-of-holding-period for purging
          sample_weight = uniqueness weights (AFML Ch.4)
    """
    if label_mode == "fill":
        # Fill probability: did the setup get filled?
        filled_types = {"filled_tp", "filled_sl", "filled_trailing",
                        "filled_timeout", "filled_guardian"}
        df = df[df["outcome_type"].notna()].copy()
        df["label"] = df["outcome_type"].isin(filled_types).astype(int)

    elif label_mode == "quality":
        # Trade quality: profitable fill or not?
        filled_types = {"filled_tp", "filled_sl", "filled_trailing",
                        "filled_timeout", "filled_guardian"}
        df = df[df["outcome_type"].isin(filled_types)].copy()
        if "pnl_pct" not in df.columns or df["pnl_pct"].isna().all():
            print("ERROR: No pnl_pct data for quality model")
            sys.exit(1)
        df["label"] = (df["pnl_pct"] > 0).astype(int)

    elif label_mode == "barrier":
        # AFML triple-barrier labels (Ch.3):
        #   TP hit -> +1, SL hit -> -1, vertical/timeout -> sign(pnl)
        filled_types = {"filled_tp", "filled_sl", "filled_trailing",
                        "filled_timeout", "filled_guardian"}
        df = df[df["outcome_type"].isin(filled_types)].copy()
        if "pnl_pct" not in df.columns or df["pnl_pct"].isna().all():
            print("ERROR: No pnl_pct data for barrier labels")
            sys.exit(1)

        def _barrier_label(row):
            ot = row["outcome_type"]
            if ot in ("filled_tp", "filled_trailing"):
                return 1   # upper barrier
            elif ot == "filled_sl":
                return -1  # lower barrier
            else:
                # vertical barrier (timeout, guardian): sign of PnL
                pnl = row.get("pnl_pct", 0) or 0
                if pnl > 0:
                    return 1
                elif pnl < 0:
                    return -1
                return 0  # breakeven

        df["label"] = df.apply(_barrier_label, axis=1)
        # Drop breakeven (0) labels — rare and uninformative (AFML Snippet 3.8)
        n_zero = (df["label"] == 0).sum()
        if n_zero > 0:
            print(f"  Dropping {n_zero} breakeven (0) labels per AFML Snippet 3.8")
            df = df[df["label"] != 0]

    else:
        raise ValueError(f"Unknown label_mode: {label_mode}")

    print(f"\nLabel distribution ({label_mode}):")
    print(df["label"].value_counts().to_string())
    print(f"Total samples: {len(df)}")

    # Drop rare label classes < 5% per AFML Snippet 3.8
    label_counts = df["label"].value_counts()
    min_count = len(df) * 0.05
    rare_labels = label_counts[label_counts < min_count].index
    if len(rare_labels) > 0:
        n_drop = df["label"].isin(rare_labels).sum()
        print(f"  WARNING: Dropping {n_drop} samples with rare labels {list(rare_labels)} (< 5%)")
        df = df[~df["label"].isin(rare_labels)]

    # Select feature columns — exclude non-stationary & metadata
    exclude_cols = {
        "id", "setup_id", "feature_version", "timestamp",
        "outcome_type", "pnl_pct", "pnl_usd", "actual_entry", "actual_exit",
        "exit_reason", "fill_duration_ms", "trade_duration_ms",
        "guardian_close_reason", "created_at", "resolved_at", "label",
        # Non-stationary absolute prices (AFML Ch.5 — leak temporal info)
        "entry_price", "sl_price", "tp1_price", "tp2_price",
        "current_price_at_detection",
        # Non-stationary raw values — use normalized alternatives instead
        # oi_usd → oi_delta_pct (already computed), cvd_raw → buy_dominance
        "oi_usd", "cvd_5m", "cvd_15m", "cvd_1h",
        # Guardian shadow flags — outcome-dependent, not available at detection
        "guardian_shadow_counter", "guardian_shadow_momentum",
        "guardian_shadow_stall", "guardian_shadow_cvd",
    }

    if label_mode in ("quality", "barrier"):
        # Exclude risk context for quality/barrier models (leakage risk)
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

    # Holding period end times for purging (AFML Ch.7)
    groups = None
    if "resolved_at" in df.columns and "created_at" in df.columns:
        df_times = df[["created_at", "resolved_at"]].copy()
        df_times["created_at"] = pd.to_datetime(df_times["created_at"])
        df_times["resolved_at"] = pd.to_datetime(df_times["resolved_at"])
        df_times["resolved_at"] = df_times["resolved_at"].fillna(
            df_times["created_at"] + pd.Timedelta(hours=12)
        )
        X.index = df_times["created_at"].values
        y.index = X.index
        groups = df_times["resolved_at"]
        groups.index = X.index

    # Sample uniqueness weights (AFML Ch.4)
    sample_weight = None
    if groups is not None:
        start_times = pd.Series(X.index, index=range(len(X)))
        end_times = pd.Series(groups.values, index=range(len(groups)))
        uniqueness = compute_sample_uniqueness(start_times, end_times)
        sample_weight = pd.Series(uniqueness.values, index=X.index)
        avg_u = sample_weight.mean()
        print(f"\nSample uniqueness (AFML Ch.4): avg={avg_u:.3f}, "
              f"min={sample_weight.min():.3f}, max={sample_weight.max():.3f}")

    return X, y, groups, sample_weight


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="ML Feature Importance Analysis (AFML Ch.7-8)")
    parser.add_argument("--min-version", type=int, default=4, help="Minimum feature version")
    parser.add_argument("--label", choices=["fill", "quality", "barrier"], default="fill",
                        help="Label mode: fill-probability, trade-quality, or triple-barrier")
    parser.add_argument("--top", type=int, default=20, help="Number of top features to show")
    parser.add_argument("--n-folds", type=int, default=5, help="CV folds")
    parser.add_argument("--embargo", type=float, default=0.02, help="Embargo fraction")
    parser.add_argument("--skip-mda", action="store_true", help="Skip MDA (slow)")
    parser.add_argument("--skip-sfi", action="store_true", help="Skip SFI (slow)")
    args = parser.parse_args()

    print("=" * 60)
    print("ML FEATURE IMPORTANCE — AFML Ch.7-8")
    print("=" * 60)

    # Load data
    df = load_ml_data(min_version=args.min_version)
    if len(df) < 10:
        print(f"\nERROR: Only {len(df)} samples — need at least 10 for analysis.")
        print("Collect more data before running feature importance.")
        sys.exit(1)

    # Prepare features
    X, y, groups, sample_weight = prepare_features(df, label_mode=args.label)
    print(f"\nFeature matrix: {X.shape[0]} samples x {X.shape[1]} features")

    # Report excluded non-stationary features
    print("\nExcluded (non-stationary / AFML Ch.5):")
    print("  entry_price, sl_price, tp1_price, tp2_price, current_price_at_detection")
    print("  oi_usd (use oi_delta_pct), cvd_5m/15m/1h (use buy_dominance)")

    if len(y.unique()) < 2:
        print("\nERROR: Only one class present — cannot train classifier.")
        sys.exit(1)

    # Classifier — AFML Ch.8: max_features=1 is CRITICAL for MDI
    # Forces each split to consider only 1 random feature so every feature
    # gets a chance to reduce impurity.
    clf = RandomForestClassifier(
        n_estimators=200,
        max_features=1,  # AFML Ch.8 requirement for unbiased MDI
        max_depth=5,
        min_samples_leaf=3,
        random_state=42,
        n_jobs=-1,
    )

    # Purged k-fold CV (AFML Ch.7)
    cv = PurgedKFoldCV(n_splits=args.n_folds, embargo_pct=args.embargo)

    # --- MDI ---
    print("\n" + "=" * 60)
    print("MDI — Mean Decrease Impurity (AFML Snippet 8.2)")
    print("  max_features=1, sample_weight=uniqueness")
    print("=" * 60)
    w = sample_weight.values if sample_weight is not None else None
    clf.fit(X, y, sample_weight=w)
    mdi = mdi_importance(clf, list(X.columns))
    print(f"\nTop {args.top} features (MDI):")
    for i, (feat, row) in enumerate(mdi.head(args.top).iterrows()):
        bar = "#" * int(row["mean"] * 200)
        print(f"  {i+1:2d}. {feat:<40s} {row['mean']:.4f} +/-{row['std']:.4f}  {bar}")

    # --- MDA ---
    if not args.skip_mda:
        print("\n" + "=" * 60)
        print("MDA — Mean Decrease Accuracy (AFML Snippet 8.3)")
        print("  Scoring: neg_log_loss, purged k-fold + embargo")
        print("=" * 60)
        mda = mda_importance(clf, X, y, cv, groups, sample_weight)
        if len(mda) > 0:
            print(f"\nTop {args.top} features (MDA — log-loss drop):")
            for i, (feat, row) in enumerate(mda.head(args.top).iterrows()):
                bar = "#" * max(0, int(row["mean"] * 500))
                print(f"  {i+1:2d}. {feat:<40s} {row['mean']:+.4f} +/-{row['std']:.4f}  {bar}")
        else:
            print("  Could not compute MDA (insufficient data per fold)")

    # --- SFI ---
    if not args.skip_sfi:
        print("\n" + "=" * 60)
        print("SFI — Single Feature Importance (AFML Snippet 8.4)")
        print("=" * 60)
        sfi = sfi_importance(clf, X, y, cv, groups, sample_weight)
        if len(sfi) > 0:
            baseline_acc = y.value_counts().max() / len(y)
            print(f"\nTop {args.top} features (SFI — standalone accuracy):")
            print(f"  Baseline (majority class): {baseline_acc:.4f}")
            for i, (feat, row) in enumerate(sfi.head(args.top).iterrows()):
                delta = row["mean"] - baseline_acc
                marker = "+" if delta > 0 else " "
                print(f"  {i+1:2d}. {feat:<40s} {row['mean']:.4f}  ({marker}{delta:.4f} vs baseline)")

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
        w_tr = sample_weight.iloc[train_idx].values if sample_weight is not None else None
        clf_cv = RandomForestClassifier(
            n_estimators=200, max_features=1, max_depth=5,
            min_samples_leaf=3, random_state=42, n_jobs=-1,
        )
        clf_cv.fit(X_tr, y_tr, sample_weight=w_tr)
        cv_scores.append(accuracy_score(y_te, clf_cv.predict(X_te)))

    if cv_scores:
        baseline_acc = y.value_counts().max() / len(y)
        mean_acc = np.mean(cv_scores)
        std_acc = np.std(cv_scores)
        print(f"  Baseline accuracy: {baseline_acc:.4f}")
        print(f"  CV accuracy:       {mean_acc:.4f} +/- {std_acc:.4f}")
        print(f"  Lift over baseline: {mean_acc - baseline_acc:+.4f}")
        if mean_acc <= baseline_acc:
            print("  WARNING: Model does not beat majority-class baseline.")
            print("  Features may not be predictive, or sample size is too small.")
    else:
        print("  Could not compute CV scores (insufficient data per fold)")

    # --- Triangulation summary ---
    if not args.skip_mda and not args.skip_sfi and len(mda) > 0 and len(sfi) > 0:
        print("\n" + "=" * 60)
        print("TRIANGULATION (AFML Ch.8 — Kendall tau)")
        print("=" * 60)
        from scipy.stats import kendalltau
        common = mdi.index.intersection(sfi.index)
        if len(common) >= 5:
            mdi_ranks = mdi.loc[common, "mean"].rank(ascending=False)
            sfi_ranks = sfi.loc[common, "mean"].rank(ascending=False)
            tau, pval = kendalltau(mdi_ranks, sfi_ranks)
            print(f"  MDI vs SFI Kendall tau: {tau:.3f} (p={pval:.3f})")
            if abs(tau) < 0.3:
                print("  LOW correlation — substitution effects likely distorting MDI")
            else:
                print("  Moderate-high correlation — feature rankings are consistent")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
