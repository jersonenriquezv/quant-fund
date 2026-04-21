#!/usr/bin/env python3
"""
Bootstrap confidence intervals for backtest results.

Takes a backtest trades CSV (from `backtest.py --csv`) and resamples trades
with replacement N times to estimate the distribution of key metrics.
Reports P5/P50/P95 confidence bounds for:
- Profit Factor
- Win Rate
- Total PnL
- Max Drawdown

Why this matters: a single backtest gives a point estimate. Point estimates
lie about risk. Bootstrapping reveals the underlying variance — a PF of 1.5
with P5=0.8 / P95=2.4 is much less reliable than PF 1.5 with P5=1.4 / P95=1.6.

Usage:
    python scripts/backtest_bootstrap.py backtest_results/trades.csv
    python scripts/backtest_bootstrap.py --iterations 5000 trades.csv
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean, median


@dataclass
class Trade:
    pair: str
    setup_type: str
    pnl_usd: float
    exit_reason: str


def load_trades(path: str) -> list[Trade]:
    trades: list[Trade] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                trades.append(Trade(
                    pair=row["pair"],
                    setup_type=row["setup_type"],
                    pnl_usd=float(row["pnl_usd"]),
                    exit_reason=row.get("exit_reason", ""),
                ))
            except (KeyError, ValueError):
                continue
    return trades


def profit_factor(trades: list[Trade]) -> float:
    wins = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    losses = abs(sum(t.pnl_usd for t in trades if t.pnl_usd < 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def win_rate(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    # Exclude flat trades (pnl ~ 0) from WR calc
    decisive = [t for t in trades if abs(t.pnl_usd) > 0.01]
    if not decisive:
        return 0.0
    return sum(1 for t in decisive if t.pnl_usd > 0) / len(decisive)


def total_pnl(trades: list[Trade]) -> float:
    return sum(t.pnl_usd for t in trades)


def max_drawdown_from_sequence(trades: list[Trade]) -> float:
    """Peak-to-trough drawdown on cumulative PnL as trades occur in order."""
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        eq += t.pnl_usd
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    return max_dd


def bootstrap(trades: list[Trade], n_iter: int, seed: int = 42) -> dict:
    """Resample trades with replacement. Return sorted arrays of each metric."""
    rng = random.Random(seed)
    n = len(trades)
    pfs: list[float] = []
    wrs: list[float] = []
    pnls: list[float] = []
    dds: list[float] = []
    for _ in range(n_iter):
        sample = [trades[rng.randint(0, n - 1)] for _ in range(n)]
        pfs.append(profit_factor(sample))
        wrs.append(win_rate(sample))
        pnls.append(total_pnl(sample))
        dds.append(max_drawdown_from_sequence(sample))
    for arr in (pfs, wrs, pnls, dds):
        arr.sort()
    return {"pf": pfs, "wr": wrs, "pnl": pnls, "dd": dds}


def percentile(arr: list[float], p: float) -> float:
    """Inclusive percentile from sorted array."""
    if not arr:
        return 0.0
    idx = max(0, min(len(arr) - 1, int(round((len(arr) - 1) * p / 100))))
    return arr[idx]


def print_report(trades: list[Trade], dists: dict) -> None:
    n = len(trades)
    point_pf = profit_factor(trades)
    point_wr = win_rate(trades)
    point_pnl = total_pnl(trades)
    point_dd = max_drawdown_from_sequence(trades)

    print()
    print("=" * 90)
    print(f"  Bootstrap Confidence Intervals  (N={n} trades, {len(dists['pf'])} resamples)")
    print("=" * 90)
    header = f"{'Metric':<18}{'Point':>12}{'P5':>12}{'P25':>12}{'P50':>12}{'P75':>12}{'P95':>12}"
    print(header)
    print("-" * 90)
    for name, key, point, fmt in [
        ("Profit Factor", "pf", point_pf, "{:.2f}"),
        ("Win Rate", "wr", point_wr, "{:.1%}"),
        ("Total PnL USD", "pnl", point_pnl, "${:.2f}"),
        ("Max Drawdown USD", "dd", point_dd, "${:.2f}"),
    ]:
        arr = dists[key]
        p5 = percentile(arr, 5)
        p25 = percentile(arr, 25)
        p50 = percentile(arr, 50)
        p75 = percentile(arr, 75)
        p95 = percentile(arr, 95)

        def fmt_v(v):
            if v == float("inf"):
                return "inf"
            return fmt.format(v)

        print(f"{name:<18}{fmt_v(point):>12}{fmt_v(p5):>12}{fmt_v(p25):>12}"
              f"{fmt_v(p50):>12}{fmt_v(p75):>12}{fmt_v(p95):>12}")
    print("=" * 90)


def per_setup_report(trades: list[Trade], n_iter: int, seed: int) -> None:
    by_setup: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_setup[t.setup_type].append(t)

    if len(by_setup) <= 1:
        return

    print()
    print("=" * 90)
    print("  Per-Setup Bootstrap (P5 / P50 / P95)")
    print("=" * 90)
    header = f"{'Setup':<20}{'N':>6}{'PF P5':>10}{'PF P50':>10}{'PF P95':>10}{'WR P50':>10}{'PnL P50':>14}"
    print(header)
    print("-" * 90)
    for setup, sub in sorted(by_setup.items()):
        if len(sub) < 5:
            print(f"{setup:<20}{len(sub):>6}  (too few trades for bootstrap)")
            continue
        dists = bootstrap(sub, n_iter, seed)
        p5 = percentile(dists["pf"], 5)
        p50 = percentile(dists["pf"], 50)
        p95 = percentile(dists["pf"], 95)
        wr50 = percentile(dists["wr"], 50)
        pnl50 = percentile(dists["pnl"], 50)

        def f(v):
            return "inf" if v == float("inf") else f"{v:.2f}"

        print(f"{setup:<20}{len(sub):>6}{f(p5):>10}{f(p50):>10}{f(p95):>10}"
              f"{wr50*100:>9.1f}% ${pnl50:>12.2f}")
    print("=" * 90)


def main():
    parser = argparse.ArgumentParser(description="Bootstrap CI for backtest results")
    parser.add_argument("csv_file", help="Trades CSV from backtest.py --csv")
    parser.add_argument("--iterations", type=int, default=2000,
                        help="Resample count (default 2000)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    trades = load_trades(args.csv_file)
    if not trades:
        print(f"No trades loaded from {args.csv_file}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(trades)} trades from {args.csv_file}")
    dists = bootstrap(trades, args.iterations, args.seed)
    print_report(trades, dists)
    per_setup_report(trades, args.iterations, args.seed)


if __name__ == "__main__":
    main()
