#!/usr/bin/env python3
"""
Regime-split report — slices shadow/live outcomes by market regime.

Uses the regime features already captured in ml_setups (volatility_regime_ratio,
trading_session, btc_return_20, btc_volatility_ratio, adx_trend_strength) to
answer: does this setup work in all regimes, or only specific ones?

Output: per-setup, per-regime win rate + profit factor + sample count. Reveals
whether a setup's edge is conditional on regime — critical before allocating
capital.

Usage:
    python scripts/backtest_regime_split.py
    python scripts/backtest_regime_split.py --experiment batch1_tp1_rr_1_3_2026_04_20
    python scripts/backtest_regime_split.py --days 60
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

from config.settings import settings


@dataclass
class Outcome:
    setup_type: str
    direction: str
    outcome_type: str
    pnl_usd: float
    vol_regime_ratio: float | None
    btc_return_20: float | None
    btc_vol_ratio: float | None
    trading_session: str | None
    adx_trend: str | None


def fetch_outcomes(days: int, experiment: str | None) -> list[Outcome]:
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=5,
    )
    conn.autocommit = True
    where = [
        "outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven',"
        "'filled_tp','filled_sl','filled_breakeven_sl','filled_trailing_sl')",
        "pnl_usd IS NOT NULL",
        f"created_at > NOW() - INTERVAL '{days} days'",
    ]
    params: list = []
    if experiment:
        where.append("experiment_id = %s")
        params.append(experiment)

    sql = f"""
        SELECT setup_type, direction, outcome_type, pnl_usd,
               volatility_regime_ratio, btc_return_20, btc_volatility_ratio,
               trading_session, adx_trend_strength
        FROM ml_setups
        WHERE {' AND '.join(where)}
    """
    out: list[Outcome] = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            out.append(Outcome(
                setup_type=row[0], direction=row[1], outcome_type=row[2],
                pnl_usd=float(row[3]),
                vol_regime_ratio=float(row[4]) if row[4] is not None else None,
                btc_return_20=float(row[5]) if row[5] is not None else None,
                btc_vol_ratio=float(row[6]) if row[6] is not None else None,
                trading_session=row[7], adx_trend=row[8],
            ))
    conn.close()
    return out


def vol_bucket(v: float | None) -> str:
    if v is None:
        return "unknown"
    if v < 0.8:
        return "low (<0.8)"
    if v < 1.2:
        return "normal (0.8-1.2)"
    return "high (>1.2)"


def btc_return_bucket(v: float | None) -> str:
    if v is None:
        return "unknown"
    if v < -0.02:
        return "down (<-2%)"
    if v > 0.02:
        return "up (>+2%)"
    return "flat (+-2%)"


def win_rate(outcomes: list[Outcome]) -> float:
    decisive = [o for o in outcomes if abs(o.pnl_usd) > 0.01]
    if not decisive:
        return 0.0
    return sum(1 for o in decisive if o.pnl_usd > 0) / len(decisive)


def pf(outcomes: list[Outcome]) -> float:
    wins = sum(o.pnl_usd for o in outcomes if o.pnl_usd > 0)
    losses = abs(sum(o.pnl_usd for o in outcomes if o.pnl_usd < 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def be_rate(outcomes: list[Outcome]) -> float:
    be = sum(1 for o in outcomes if abs(o.pnl_usd) < 0.01 or "breakeven" in o.outcome_type)
    return be / len(outcomes) if outcomes else 0.0


def total_pnl(outcomes: list[Outcome]) -> float:
    return sum(o.pnl_usd for o in outcomes)


def print_slice(title: str, groups: dict[tuple, list[Outcome]]) -> None:
    print()
    print("=" * 110)
    print(f"  {title}")
    print("=" * 110)
    print(f"{'Key':<40}{'N':>6}{'WR':>9}{'BE%':>8}{'PF':>8}{'PnL':>14}")
    print("-" * 110)
    for key in sorted(groups.keys(), key=lambda k: (k[0], str(k[1:]))):
        sub = groups[key]
        if len(sub) < 3:
            label = " / ".join(str(p) for p in key)
            print(f"{label:<40}{len(sub):>6}   (too few)")
            continue
        label = " / ".join(str(p) for p in key)
        pfv = pf(sub)
        pf_str = "inf" if pfv == float("inf") else f"{pfv:.2f}"
        print(f"{label:<40}{len(sub):>6}{win_rate(sub)*100:>8.1f}%"
              f"{be_rate(sub)*100:>7.1f}%{pf_str:>8}${total_pnl(sub):>12.2f}")
    print("=" * 110)


def main():
    parser = argparse.ArgumentParser(description="Regime-split analysis of ml_setups outcomes")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--experiment", type=str, default=None,
                        help="Filter by experiment_id (e.g. batch1_tp1_rr_1_3_2026_04_20)")
    args = parser.parse_args()

    outcomes = fetch_outcomes(args.days, args.experiment)
    print(f"Loaded {len(outcomes)} outcomes "
          f"(last {args.days} days"
          f"{', experiment=' + args.experiment if args.experiment else ''}).")
    if not outcomes:
        sys.exit(0)

    # Slice 1: setup × volatility regime
    g = defaultdict(list)
    for o in outcomes:
        g[(o.setup_type, vol_bucket(o.vol_regime_ratio))].append(o)
    print_slice("Setup × Volatility Regime", g)

    # Slice 2: setup × trading session
    g = defaultdict(list)
    for o in outcomes:
        g[(o.setup_type, o.trading_session or "unknown")].append(o)
    print_slice("Setup × Trading Session", g)

    # Slice 3: setup × BTC 20-candle return bucket
    g = defaultdict(list)
    for o in outcomes:
        g[(o.setup_type, btc_return_bucket(o.btc_return_20))].append(o)
    print_slice("Setup × BTC 20-bar Return", g)

    # Slice 4: setup × direction
    g = defaultdict(list)
    for o in outcomes:
        g[(o.setup_type, o.direction)].append(o)
    print_slice("Setup × Direction", g)

    # Slice 5: setup × ADX trend strength
    g = defaultdict(list)
    for o in outcomes:
        g[(o.setup_type, o.adx_trend or "unknown")].append(o)
    print_slice("Setup × ADX Trend Strength", g)


if __name__ == "__main__":
    main()
