#!/usr/bin/env python3
"""Forward validation — score NEW engine1 trades with the frozen model.

Loads the frozen engine1 meta-label model and evaluates it ONLY on trades that
resolved AFTER the freeze cutoff (genuinely unseen, real forward). Reports the
forward profit factor of take-all vs the model's top-half by score. This is the
final gate the OOF + out-of-time tests can only approximate — the impulse-gate
passed in-sample and died here, so this is the one that decides real money.

Designed to run on a daily systemd timer; sends a Telegram milestone when the
forward sample reaches N_GATE so you don't have to poll.

Run:  python scripts/ml_v1_forward_check.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from scripts.ml_v0_engine1 import prepare_features  # noqa: E402

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "engine1_meta_v1.pkl"
N_GATE = int(os.environ.get("ML_FWD_GATE", "40"))   # min forward trades before a verdict


def _telegram(text: str) -> None:
    token = settings.TELEGRAM_BOT_TOKEN
    chat = settings.TELEGRAM_CHAT_ID
    if not token or not chat:
        return
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
    except Exception as e:
        print(f"telegram failed: {e}", file=sys.stderr)


def main() -> int:
    if not MODEL_PATH.exists():
        print(f"ERROR: no frozen model at {MODEL_PATH}. Run ml_v1_freeze_model.py first.")
        return 1
    # joblib.load is safe here: the file is produced locally by
    # ml_v1_freeze_model.py and never sourced externally (no untrusted pickle).
    b = joblib.load(MODEL_PATH)
    cutoff = b["cutoff_created_at"]

    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=10)
    conn.set_session(readonly=True, autocommit=True)
    df = pd.read_sql("""
        SELECT * FROM ml_setups
        WHERE setup_type = 'engine1_trend_pullback'
          AND feature_version >= 4
          AND outcome_type IN ('shadow_tp','shadow_sl')
          AND (data_quality IS NULL OR data_quality <> 'partial_candle_risk')
          AND created_at > %s
        ORDER BY created_at
    """, conn, params=(cutoff,))
    conn.close()

    nf = len(df)
    print(f"forward check — frozen cutoff {cutoff}, N_gate {N_GATE}")
    if nf == 0:
        print(f"  0 forward trades yet. Accumulating. (engine1 resolves ~12h.)")
        return 0

    pnl = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0).values
    X, _, _ = prepare_features(df)
    X = X.reindex(columns=b["feature_names"])
    score = b["model"].predict_proba(X)[:, 1]

    order = np.argsort(-score)
    ps = pnl[order]
    k = max(1, nf // 2)

    def pf(a):
        l = abs(a[a < 0].sum())
        return a[a > 0].sum() / l if l > 0 else float("inf")

    all_pf, top_pf, bot_pf = pf(pnl), pf(ps[:k]), pf(ps[k:])
    pfs = lambda x: "inf" if x == float("inf") else f"{x:.2f}"
    print(f"  forward N={nf}  (need {N_GATE} for a verdict)")
    print(f"  take all:   WR {(pnl>0).mean()*100:4.1f}%  PF {pfs(all_pf)}  ${pnl.sum():+.2f}")
    print(f"  top half:   WR {(ps[:k]>0).mean()*100:4.1f}%  PF {pfs(top_pf)}  ${ps[:k].sum():+.2f}")
    print(f"  bottom half:WR {(ps[k:]>0).mean()*100:4.1f}%  PF {pfs(bot_pf)}  ${ps[k:].sum():+.2f}")

    if nf < N_GATE:
        print(f"  STATUS: accumulating {nf}/{N_GATE} — no verdict yet.")
        return 0

    holds = top_pf > max(all_pf, 1.0) and ps[:k].sum() > 0
    if holds:
        verdict = "PASS — filter holds FORWARD. Real lever. Next: calibration + small live."
    else:
        verdict = "FAIL — filter did not hold forward (impulse-gate trap). Do NOT size on it."
    print(f"  VERDICT @ N={nf}: {verdict}")

    # Telegram milestone once (state file guards against repeat spam).
    flag = Path("/tmp/ml_fwd_gate_notified")
    if not flag.exists():
        _telegram(
            f"\U0001f9ea <b>ML forward gate reached</b> (engine1)\n"
            f"forward N={nf}\n"
            f"take-all PF {pfs(all_pf)} → top-half PF {pfs(top_pf)}\n"
            f"{verdict}")
        flag.write_text(cutoff)
    return 0


if __name__ == "__main__":
    sys.exit(main())
