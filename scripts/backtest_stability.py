#!/usr/bin/env python3
"""
Stability analyzer — splits a trades CSV chronologically into N windows
and reports metrics per window. Reveals whether performance is consistent
across time, or whether the strategy only worked in one specific period.

Catches the classic overfit failure: "backtest PF=2.0 for 90 days" that
was actually PF=5 in first month and PF=0.5 in the last two. Without this
split, averages hide collapsing edges.

Usage:
    python scripts/backtest_stability.py backtest_results/trades.csv
    python scripts/backtest_stability.py --windows 6 trades.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Trade:
    entry_ts: int
    pair: str
    setup_type: str
    direction: str
    pnl_usd: float
    exit_reason: str


def _parse_ts(s: str) -> int:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    try:
        return int(float(s))
    except ValueError:
        return 0


def load_trades(path: str) -> list[Trade]:
    trades: list[Trade] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                trades.append(Trade(
                    entry_ts=_parse_ts(row["entry_time"]),
                    pair=row["pair"],
                    setup_type=row["setup_type"],
                    direction=row["direction"],
                    pnl_usd=float(row["pnl_usd"]),
                    exit_reason=row.get("exit_reason", ""),
                ))
            except (KeyError, ValueError):
                continue
    trades.sort(key=lambda t: t.entry_ts)
    return trades


def split_by_count(trades: list[Trade], n: int) -> list[list[Trade]]:
    size = len(trades) // n
    rem = len(trades) % n
    out = []
    i = 0
    for k in range(n):
        extra = 1 if k < rem else 0
        chunk = trades[i:i + size + extra]
        out.append(chunk)
        i += size + extra
    return out


def win_rate(trades: list[Trade]) -> float:
    decisive = [t for t in trades if abs(t.pnl_usd) > 0.01]
    if not decisive:
        return 0.0
    return sum(1 for t in decisive if t.pnl_usd > 0) / len(decisive)


def pf(trades: list[Trade]) -> float:
    wins = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = abs(sum(t.pnl_usd for t in trades if t.pnl_usd < 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def max_dd(trades: list[Trade]) -> float:
    eq = 0.0
    peak = 0.0
    worst = 0.0
    for t in trades:
        eq += t.pnl_usd
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > worst:
            worst = dd
    return worst


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def print_report(trades: list[Trade], windows: int) -> None:
    splits = split_by_count(trades, windows)
    print()
    print("=" * 100)
    print(f"  Chronological Stability Analysis  (N={len(trades)} trades, {windows} windows)")
    print("=" * 100)
    print(f"{'Window':<8}{'Date Range':<26}{'N':>5}{'WR':>9}{'PF':>8}{'PnL USD':>13}{'Max DD':>11}{'R multiple':>12}")
    print("-" * 100)

    for i, w in enumerate(splits, 1):
        if not w:
            print(f"{i:<8}{'(empty)':<26}{0:>5}")
            continue
        date_range = f"{_date(w[0].entry_ts)} → {_date(w[-1].entry_ts)}"
        pfv = pf(w)
        pf_str = "inf" if pfv == float("inf") else f"{pfv:.2f}"
        pnl = sum(t.pnl_usd for t in w)
        dd = max_dd(w)
        avg_r = pnl / len(w) if w else 0.0
        print(f"{i:<8}{date_range:<26}{len(w):>5}{win_rate(w)*100:>8.1f}%"
              f"{pf_str:>8}${pnl:>11.2f}${dd:>9.2f}${avg_r:>10.2f}")

    print("=" * 100)

    # Stability summary
    pfs = [pf(w) for w in splits if w and pf(w) != float("inf")]
    wrs = [win_rate(w) for w in splits if w]
    if pfs and wrs:
        import statistics
        pf_mean = statistics.mean(pfs)
        pf_std = statistics.pstdev(pfs) if len(pfs) > 1 else 0.0
        wr_mean = statistics.mean(wrs)
        wr_std = statistics.pstdev(wrs) if len(wrs) > 1 else 0.0
        cv_pf = pf_std / pf_mean if pf_mean else 0.0
        cv_wr = wr_std / wr_mean if wr_mean else 0.0
        print()
        print(f"  PF across windows: mean={pf_mean:.2f}  std={pf_std:.2f}  "
              f"CV={cv_pf:.2f}  {'STABLE' if cv_pf < 0.3 else 'UNSTABLE' if cv_pf > 0.7 else 'moderate'}")
        print(f"  WR across windows: mean={wr_mean*100:.1f}%  std={wr_std*100:.1f}%  "
              f"CV={cv_wr:.2f}  {'STABLE' if cv_wr < 0.2 else 'UNSTABLE' if cv_wr > 0.5 else 'moderate'}")
        print()
        print("  Coefficient of Variation guide: CV < 0.3 for PF / 0.2 for WR = stable edge.")
        print("  CV > 0.7 / 0.5 = edge concentrated in one window. Beware overfit.")
    print("=" * 100)


def main():
    parser = argparse.ArgumentParser(description="Chronological stability split")
    parser.add_argument("csv_file")
    parser.add_argument("--windows", type=int, default=4,
                        help="Number of chronological windows (default 4 = quartiles)")
    args = parser.parse_args()

    trades = load_trades(args.csv_file)
    if not trades:
        print(f"No trades loaded from {args.csv_file}", file=sys.stderr)
        sys.exit(1)
    if len(trades) < args.windows * 3:
        print(f"WARN: only {len(trades)} trades for {args.windows} windows — "
              f"results will be noisy", file=sys.stderr)
    print_report(trades, args.windows)


if __name__ == "__main__":
    main()
