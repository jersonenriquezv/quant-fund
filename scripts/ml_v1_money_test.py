#!/usr/bin/env python3
"""ML v1 money test — does filtering engine1 by model score make it profitable?

Bridges AUC -> money. AUC 0.74 says the meta-label model RANKS engine1 trades
well. This asks the only question that matters: if engine1 had only taken the
trades the model scored highly, would the paper PnL / profit factor actually
improve, or is the ranking skill economically useless?

Method (rank-based, so robust to the model's poor calibration / Brier 0.234):
  1. Out-of-fold scores from the SAME purged k-fold CV as ml_v1_meta_label
     (no row scored by a model that saw it — no leakage).
  2. Join each engine1 shadow trade's OOF score with its realized paper PnL
     (pnl_usd, already net of taker fees per shared/pnl_engine).
  3. Bucket by score and report WR / Profit Factor / total PnL / expectancy for
     take-all vs top-half vs bottom-half vs top-tercile, plus a threshold sweep.

A real, monetizable filter => top buckets have PF > 1 and clearly beat take-all,
bottom bucket is where the losses concentrate. If top and bottom look the same,
the 0.74 ranking does not convert to money.

Run:  python scripts/ml_v1_money_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.feature_importance import PurgedKFoldCV, compute_sample_uniqueness  # noqa: E402
from scripts.ml_v0_engine1 import prepare_features  # noqa: E402
from scripts.ml_v1_meta_label import fetch_for, LGB_PARAMS, N_SPLITS, EMBARGO_PCT  # noqa: E402


def bucket_stats(name: str, pnl: np.ndarray) -> dict:
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_w = wins.sum()
    gross_l = abs(losses.sum())
    pf = gross_w / gross_l if gross_l > 0 else float("inf")
    return {
        "name": name,
        "n": len(pnl),
        "wr": (pnl > 0).mean() * 100 if len(pnl) else 0.0,
        "pf": pf,
        "total": pnl.sum(),
        "exp": pnl.mean() if len(pnl) else 0.0,
    }


def line(s: dict) -> str:
    pf = "inf " if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"  {s['name']:<22} N={s['n']:<4} WR={s['wr']:4.1f}%  "
            f"PF={pf:<5}  total=${s['total']:+8.2f}  exp/trade=${s['exp']:+6.2f}")


def main() -> int:
    df = fetch_for("engine1_trend_pullback")
    if len(df) < 40:
        print(f"ERROR: {len(df)} rows, need >= 40")
        return 1

    start_t = pd.to_datetime(df["created_at"]).reset_index(drop=True)
    end_t = pd.to_datetime(df["resolved_at"]).reset_index(drop=True).fillna(start_t)
    pnl = pd.to_numeric(df["pnl_usd"], errors="coerce").reset_index(drop=True)

    X, y, cat_cols = prepare_features(df)
    X = X.reset_index(drop=True); y = y.reset_index(drop=True)
    weights = compute_sample_uniqueness(start_t, end_t).reset_index(drop=True)
    X.index = start_t.values
    groups = pd.Series(end_t.values, index=X.index)

    # Out-of-fold scores from purged CV.
    cv = PurgedKFoldCV(n_splits=N_SPLITS, embargo_pct=EMBARGO_PCT)
    oof = np.full(len(X), np.nan)
    for tr_idx, te_idx in cv.split(X, y, groups):
        if len(tr_idx) == 0 or len(te_idx) == 0 or y.iloc[tr_idx].nunique() < 2:
            continue
        clf = lgb.LGBMClassifier(**LGB_PARAMS)
        clf.fit(X.iloc[tr_idx], y.iloc[tr_idx],
                sample_weight=weights.iloc[tr_idx].values,
                categorical_feature=cat_cols or "auto")
        oof[te_idx] = clf.predict_proba(X.iloc[te_idx])[:, 1]

    m = ~np.isnan(oof) & pnl.notna().values
    score = oof[m]
    p = pnl[m].values
    n = len(p)
    order = np.argsort(-score)  # high score first
    p_sorted = p[order]

    print(f"ml_v1 money test — engine1, N={n} scored trades (paper PnL, net of fees)")
    print(f"  PnL is theoretical shadow sizing. Rank-based test (calibration-agnostic).")
    print()

    all_s = bucket_stats("TAKE ALL (baseline)", p)
    top_half = bucket_stats("top half (score)", p_sorted[: n // 2])
    bot_half = bucket_stats("bottom half (score)", p_sorted[n // 2:])
    top_terc = bucket_stats("top tercile", p_sorted[: n // 3])
    bot_terc = bucket_stats("bottom tercile", p_sorted[-(n // 3):])

    for s in (all_s, top_half, bot_half, top_terc, bot_terc):
        print(line(s))

    print()
    print("  Threshold sweep — take only the top X% by score:")
    print(f"  {'keep':<8}{'N':<6}{'WR':<8}{'PF':<8}{'total':<12}{'exp/trade'}")
    for frac in (1.0, 0.75, 0.5, 0.33, 0.25, 0.10):
        k = max(1, int(n * frac))
        s = bucket_stats(f"top {int(frac*100)}%", p_sorted[:k])
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"  {int(frac*100):>3}%    {s['n']:<6}{s['wr']:<7.1f}%{pf:<8}${s['total']:<10.2f}${s['exp']:+.2f}")

    print()
    # Verdict
    improves = top_half["pf"] > all_s["pf"] and top_half["total"] > 0
    concentrates = bot_half["pf"] < all_s["pf"]
    if improves and concentrates:
        v = ("MONETIZABLE — top-score trades beat take-all and turn positive; "
             "losses concentrate in low scores. The 0.74 ranking IS a usable filter.")
    elif top_half["total"] > 0 and all_s["total"] <= 0:
        v = ("PROMISING — filtering flips engine1 from losing to positive, "
             "though the top/bottom split is not clean. Worth a forward test.")
    else:
        v = ("NOT MONETIZABLE (yet) — high-score trades do not clearly beat "
             "low-score ones in PnL. Ranking skill does not convert to money at this N.")
    print(f"  VERDICT: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
