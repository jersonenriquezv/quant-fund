"""Lane A exploratory analysis — engine1 entry-gate candidates.

READ-ONLY. Does not touch strategy_service, settings, or any live path.
Slices resolved engine1_trend_pullback shadow outcomes by the features the
ml_v0 model ranked highest, looking for a subset whose profit factor (PF)
materially beats the full-cohort baseline.

This is data dredging by construction (many slices tested) — treat any
finding as a HYPOTHESIS to grill, never as proven edge. A candidate only
matters if it (a) holds on the current v1d regime, not just the pooled set,
and (b) keeps enough N to be believable.

Usage: PYTHONPATH=. python scripts/analyze_engine1_entry_gates.py
"""
import psycopg2
import pandas as pd
from config.settings import settings

CONTINUOUS = [
    "engine1_pullback_depth_pct", "engine1_entry_atr_distance",
    "engine1_impulse_atr_multiple", "wt_wt2", "setup_age_minutes",
    "engine1_pullback_candle_count", "risk_distance_pct", "funding_rate",
    "rsi_14", "bb_percent_b",
]
V1D = "engine1_short_quarantine_v1d_2026_05_22"
RESOLVED = ("shadow_tp", "shadow_sl", "shadow_breakeven", "shadow_timeout")


def metrics(df):
    """WR (% pnl>0) and PF (gross win / gross loss) over a frame with pnl_usd."""
    n = len(df)
    if n == 0:
        return 0, None, None
    wins = df.loc[df.pnl_usd > 0, "pnl_usd"].sum()
    loss = -df.loc[df.pnl_usd < 0, "pnl_usd"].sum()
    wr = round(100 * (df.pnl_usd > 0).mean(), 1)
    pf = round(wins / loss, 2) if loss > 0 else None
    return n, wr, pf


def main():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=10,
    )
    conn.set_session(readonly=True, autocommit=True)
    df = pd.read_sql(
        """SELECT *, experiment_id AS exp FROM ml_setups
           WHERE setup_type='engine1_trend_pullback' AND feature_version>=4
             AND outcome_type IN %s AND pnl_usd IS NOT NULL""",
        conn, params=(RESOLVED,),
    )
    conn.close()

    for label, frame in [("POOLED (all regimes)", df),
                         ("V1D ONLY (current)", df[df.exp == V1D])]:
        n, wr, pf = metrics(frame)
        print(f"\n{'='*64}\n{label}  —  baseline: N={n} WR={wr}% PF={pf}\n{'='*64}")
        rows = []
        for feat in CONTINUOUS:
            sub = frame[frame[feat].notna()]
            if len(sub) < 60:
                continue
            try:
                sub = sub.assign(_b=pd.qcut(sub[feat], 3, labels=["low", "mid", "high"], duplicates="drop"))
            except ValueError:
                continue
            for b in sub["_b"].cat.categories:
                bn, bwr, bpf = metrics(sub[sub._b == b])
                if bn >= 30 and bpf is not None:
                    rows.append((feat, b, bn, bwr, bpf))
        # show only buckets that beat baseline PF, sorted by PF
        base_pf = pf or 0
        rows = [r for r in rows if r[4] > base_pf]
        rows.sort(key=lambda r: -r[4])
        if not rows:
            print("  no bucket beats baseline PF at N>=30")
            continue
        print(f"  {'feature':32} {'bucket':5} {'N':>4} {'WR%':>6} {'PF':>6}")
        for feat, b, bn, bwr, bpf in rows:
            print(f"  {feat:32} {b:5} {bn:>4} {bwr:>6} {bpf:>6}")


if __name__ == "__main__":
    main()
