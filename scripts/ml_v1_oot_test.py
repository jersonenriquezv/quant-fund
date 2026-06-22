#!/usr/bin/env python3
"""ML v1 out-of-time test — does the score filter hold on LATER trades?

Stronger than the OOF money test against the hindsight worry: train the model
ONLY on the oldest 70% of engine1 trades, then score the newest 30% (which are
strictly later in time and unseen). If filtering those future trades by score
still lifts the profit factor, the edge is not just an in-sample artifact.

Run:  python scripts/ml_v1_oot_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.feature_importance import compute_sample_uniqueness  # noqa: E402
from scripts.ml_v0_engine1 import prepare_features  # noqa: E402
from scripts.ml_v1_meta_label import fetch_for, LGB_PARAMS  # noqa: E402

TRAIN_FRAC = 0.70


def pf_stats(name, pnl):
    w = pnl[pnl > 0].sum(); l = abs(pnl[pnl < 0].sum())
    pf = w / l if l > 0 else float("inf")
    pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (f"  {name:<20} N={len(pnl):<4} WR={ (pnl>0).mean()*100:4.1f}%  "
            f"PF={pfs:<5} total=${pnl.sum():+8.2f}")


def main() -> int:
    df = fetch_for("engine1_trend_pullback")  # sorted by created_at
    start_t = pd.to_datetime(df["created_at"]).reset_index(drop=True)
    end_t = pd.to_datetime(df["resolved_at"]).reset_index(drop=True).fillna(start_t)
    pnl = pd.to_numeric(df["pnl_usd"], errors="coerce").reset_index(drop=True)
    X, y, cat = prepare_features(df)
    X = X.reset_index(drop=True); y = y.reset_index(drop=True)
    w = compute_sample_uniqueness(start_t, end_t).reset_index(drop=True)

    n = len(df); ntr = int(n * TRAIN_FRAC)
    tr, te = slice(0, ntr), slice(ntr, n)

    clf = lgb.LGBMClassifier(**LGB_PARAMS)
    clf.fit(X.iloc[tr], y.iloc[tr], sample_weight=w.iloc[tr].values,
            categorical_feature=cat or "auto")
    score = clf.predict_proba(X.iloc[te])[:, 1]

    p = pnl.iloc[te].values
    order = np.argsort(-score)
    p_sorted = p[order]
    k = len(p) // 2

    print(f"out-of-time — train oldest {ntr} (to {start_t.iloc[ntr-1].date()}), "
          f"test newest {n-ntr} (from {start_t.iloc[ntr].date()})")
    print(f"  trades unseen + strictly later in time. paper PnL, net of fees.\n")
    print(pf_stats("TAKE ALL (newest)", p))
    print(pf_stats("top half by score", p_sorted[:k]))
    print(pf_stats("bottom half by score", p_sorted[k:]))

    all_pf = (p[p>0].sum() / abs(p[p<0].sum())) if (p<0).any() else float("inf")
    top_pf = (p_sorted[:k][p_sorted[:k]>0].sum() / abs(p_sorted[:k][p_sorted[:k]<0].sum())) \
        if (p_sorted[:k]<0).any() else float("inf")
    print()
    if top_pf > max(all_pf, 1.0) and p_sorted[:k].sum() > 0:
        print("  VERDICT: HOLDS out-of-time — filter still works on later unseen trades. "
              "Real forward test next.")
    else:
        print("  VERDICT: DOES NOT HOLD out-of-time — edge was in-sample. "
              "Same trap as the impulse-gate. Do NOT size on it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
