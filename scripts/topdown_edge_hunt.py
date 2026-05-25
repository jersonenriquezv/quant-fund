"""One-off investigation: where (if anywhere) does /topdown edge live?

Reads the existing backtest trades CSV + cascade events CSV. No new backtest run.
Answers, with train/holdout discipline and expectancy-in-R (not just WR, since
R:R varies a lot per trade):

  1. tp_mode breakdown — is 'scaled' really 0% WR? (suspected sim artifact)
  2. sweep_distance buckets — WR + expectancy, train(first 70%) vs holdout(last 30%)
  3. direction + pair breakdown
  4. VICTIM CROSS — does a /topdown emission near a recent large liquidation
     cascade (same pair) win more than one without?
"""
import sys
import pandas as pd
import numpy as np

TRADES = "backtest_results/topdown_20260525_022050_trades.csv"
CASCADES = "backtest_results/cascade_20260525_042151_events.csv"


def _wr_exp(df: pd.DataFrame) -> tuple[int, int, float, float]:
    """Resolved-only WR + mean expectancy in R. tp -> +rr, sl -> -1R."""
    res = df[df["outcome"].isin(["tp", "sl"])].copy()
    n = len(res)
    if n == 0:
        return 0, 0, float("nan"), float("nan")
    tp = int((res["outcome"] == "tp").sum())
    wr = 100.0 * tp / n
    res["R"] = np.where(res["outcome"] == "tp", res["rr"], -1.0)
    return n, tp, wr, res["R"].mean()


def line(label, df):
    n, tp, wr, exp = _wr_exp(df)
    print(f"  {label:<26} N={n:<5} TP={tp:<4} WR={wr:5.1f}%  E={exp:+.3f}R")


def main() -> int:
    t = pd.read_csv(TRADES)
    print(f"=== loaded {len(t)} emissions ===")
    print(f"outcome dist: {t['outcome'].value_counts().to_dict()}")

    # chronological split
    cutoff = t["t_ms"].quantile(0.70)
    train = t[t["t_ms"] <= cutoff]
    hold = t[t["t_ms"] > cutoff]

    print("\n### 1. tp_mode breakdown")
    for mode in t["tp_mode"].unique():
        line(f"mode={mode}", t[t["tp_mode"] == mode])
    print("  outcome dist per mode:")
    print(t.groupby("tp_mode")["outcome"].value_counts().to_string())

    print("\n### 2. sweep_distance buckets (single-mode only, full)")
    s = t[t["tp_mode"] == "single"].copy()
    bins = [0, 0.5, 1.0, 2.0, 3.0, 5.0, 100]
    labels = ["0-0.5", "0.5-1", "1-2", "2-3", "3-5", "5+"]
    s["bucket"] = pd.cut(s["sweep_distance_pct"], bins=bins, labels=labels)
    for b in labels:
        line(f"sweep {b}%", s[s["bucket"] == b])
    print("  -- TRAIN vs HOLDOUT for tight (<=1%) vs loose (>1%):")
    for name, dd in [("train", train), ("hold", hold)]:
        ds = dd[dd["tp_mode"] == "single"]
        line(f"{name} sweep<=1%", ds[ds["sweep_distance_pct"] <= 1.0])
        line(f"{name} sweep>1%", ds[ds["sweep_distance_pct"] > 1.0])

    print("\n### 3. direction + pair (single-mode)")
    for d in ["long", "short"]:
        line(f"dir={d}", s[s["direction"] == d])
    for p in sorted(s["pair"].unique()):
        line(f"pair={p}", s[s["pair"] == p])

    print("\n### 4. VICTIM CROSS — cascade near entry")
    c = pd.read_csv(CASCADES)
    # reversion side: long_liq cascade (price dumped, longs liquidated) -> expect
    # bounce UP -> favors a LONG entry. short_liq -> favors SHORT.
    c["fav_dir"] = np.where(c["liq_side"] == "long_liq", "long", "short")
    # large = the bucket that reverts per the cascade grill (>2.5%); also test >=1.5%
    for win_h in [3, 6, 12]:
        for min_move, tag in [(1.5, ">=1.5%"), (2.5, ">=2.5%")]:
            win_ms = win_h * 3600 * 1000
            big = c[c["price_move_pct"].abs() >= min_move]
            matched = []
            for _, tr in s.iterrows():
                near = big[(big["pair"] == tr["pair"]) &
                           (big["cascade_ts"] <= tr["t_ms"]) &
                           (big["cascade_ts"] >= tr["t_ms"] - win_ms)]
                if len(near) == 0:
                    matched.append(0)
                    continue
                # aligned if any near cascade's reversion favours the trade dir
                matched.append(int((near["fav_dir"] == tr["direction"]).any()))
            s["_casc"] = matched
            with_c = s[s["_casc"] == 1]
            if len(with_c[with_c["outcome"].isin(["tp", "sl"])]) >= 8:
                print(f"  win={win_h}h move{tag}:")
                line("    cascade-aligned", with_c)
                line("    no-cascade", s[s["_casc"] == 0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
