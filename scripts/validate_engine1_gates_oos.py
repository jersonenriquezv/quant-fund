"""Grill evidence — out-of-sample validation of the 3 engine1 entry-gate candidates.

READ-ONLY. The discovery (analyze_engine1_entry_gates.py) was in-sample data
dredging. This script answers the only question that matters: do the gates
survive on data they were NOT discovered on?

Method (v1d regime, chronological — no shuffling, real time order):
  1. Split rows 70/30 by created_at (train = older, test = newer).
  2. Learn the tercile cutoffs ONLY from train.
  3. Apply those frozen cutoffs to test. Measure PF/WR on test vs test baseline.
  4. Also test the COMBINED gate (all 3 conditions) — the actual proposal.

A gate survives only if test PF clearly beats test baseline AND keeps N.
"""
import psycopg2
import pandas as pd
from config.settings import settings

V1D = "engine1_short_quarantine_v1d_2026_05_22"
RESOLVED = ("shadow_tp", "shadow_sl", "shadow_breakeven", "shadow_timeout")
# (feature, which tercile side wins) discovered in-sample
GATES = [
    ("engine1_impulse_atr_multiple", "low"),
    ("engine1_pullback_depth_pct", "low"),
    ("engine1_entry_atr_distance", "high"),
]


def metrics(df):
    n = len(df)
    if n == 0:
        return 0, None, None
    loss = -df.loc[df.pnl_usd < 0, "pnl_usd"].sum()
    win = df.loc[df.pnl_usd > 0, "pnl_usd"].sum()
    wr = round(100 * (df.pnl_usd > 0).mean(), 1)
    pf = round(win / loss, 2) if loss > 0 else None
    return n, wr, pf


def main():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=10,
    )
    conn.set_session(readonly=True, autocommit=True)
    df = pd.read_sql(
        """SELECT * FROM ml_setups
           WHERE setup_type='engine1_trend_pullback' AND feature_version>=4
             AND experiment_id=%s AND outcome_type IN %s AND pnl_usd IS NOT NULL
           ORDER BY created_at""",
        conn, params=(V1D, RESOLVED),
    )
    conn.close()

    cut = int(len(df) * 0.70)
    train, test = df.iloc[:cut], df.iloc[cut:]
    bn, bwr, bpf = metrics(test)
    print(f"v1d N={len(df)}  train={len(train)}  test={len(test)}")
    print(f"TEST baseline: N={bn} WR={bwr}% PF={bpf}\n")

    # learn cutoffs from train (33rd / 67th percentile), freeze, apply to test
    masks = []
    print(f"{'gate':40} {'cutoff(train)':>16} {'testN':>6} {'WR%':>6} {'PF':>6}")
    for feat, side in GATES:
        q = train[feat].quantile(0.33 if side == "low" else 0.67)
        m = test[feat] <= q if side == "low" else test[feat] >= q
        masks.append(m)
        n, wr, pf = metrics(test[m])
        print(f"{feat+' ('+side+')':40} {round(q,4):>16} {n:>6} {wr:>6} {str(pf):>6}")

    combined = masks[0] & masks[1] & masks[2]
    n, wr, pf = metrics(test[combined])
    print(f"\n{'COMBINED (all 3)':40} {'':>16} {n:>6} {wr:>6} {str(pf):>6}")
    # pairwise too — combined may starve N
    for i, j in [(0, 1), (0, 2), (1, 2)]:
        m = masks[i] & masks[j]
        n, wr, pf = metrics(test[m])
        tag = f"{GATES[i][0].split('_')[-1]}+{GATES[j][0].split('_')[-1]}"
        print(f"{'PAIR '+tag:40} {'':>16} {n:>6} {wr:>6} {str(pf):>6}")


if __name__ == "__main__":
    main()
