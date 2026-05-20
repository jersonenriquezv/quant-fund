"""Counterfactual study: would a 1D HTF bias veto have improved engine1?

Read-only. For each engine1 outcome in the window, reconstruct what the 1D
market structure would have shown at the entry moment, then compare to the
trade direction. Cross-tab by outcome class.

Decision rule from docs/grill/1d-htf-veto-layer-2026-05-20.md Q1:
- wrong_direction SLs >60% disagree-1D AND TPs <30% disagree-1D → veto informative
- Both similar (~40-50%) → 1D adds no signal
- wrong_direction <60% disagree-1D → weak

Run: PYTHONPATH=. python scripts/study_1d_veto.py
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import psycopg2

from config.settings import settings
from shared.models import Candle
from strategy_service.market_structure import MarketStructureAnalyzer


@dataclass
class Outcome:
    setup_id: str
    pair: str
    direction: str
    outcome_type: str
    htf_bias: Optional[str]
    fill_ts: int
    mfe_r: Optional[float]
    mae_r: Optional[float]


def _connect():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _pull_outcomes(cur, days: int) -> list[Outcome]:
    cur.execute(
        """
        SELECT setup_id, pair, direction, outcome_type, htf_bias,
               COALESCE(shadow_fill_candle_ts,
                        EXTRACT(EPOCH FROM created_at)*1000) AS fill_ts
        FROM ml_setups
        WHERE setup_type = 'engine1_trend_pullback'
          AND outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven')
          AND feature_version >= 4
          AND created_at >= NOW() - (%s || ' days')::interval
          AND COALESCE(shadow_fill_candle_ts, 0) > 0
        ORDER BY created_at DESC
        """,
        (days,),
    )
    return [
        Outcome(
            setup_id=r[0], pair=r[1], direction=r[2], outcome_type=r[3],
            htf_bias=r[4], fill_ts=int(r[5]),
            mfe_r=None, mae_r=None,
        )
        for r in cur.fetchall()
    ]


def _load_1d_candles_before(cur, pair: str, ts: int,
                            limit: int = 60) -> list[Candle]:
    cur.execute(
        """
        SELECT timestamp, open, high, low, close, volume, volume_quote
        FROM candles
        WHERE pair = %s AND timeframe = '1d' AND timestamp < %s
        ORDER BY timestamp DESC
        LIMIT %s
        """,
        (pair, ts, limit),
    )
    rows = cur.fetchall()
    return [
        Candle(
            timestamp=int(r[0]), open=float(r[1]), high=float(r[2]),
            low=float(r[3]), close=float(r[4]), volume=float(r[5]),
            volume_quote=float(r[6]) if r[6] is not None else 0.0,
            pair=pair, timeframe="1d", confirmed=True,
        )
        for r in reversed(rows)  # oldest first
    ]


def _agreement(direction: str, htf_trend: str) -> str:
    if htf_trend == "undefined":
        return "undefined"
    if direction == "long" and htf_trend == "bullish":
        return "agree"
    if direction == "short" and htf_trend == "bearish":
        return "agree"
    return "disagree"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    conn = _connect()
    cur = conn.cursor()
    outcomes = _pull_outcomes(cur, args.days)
    if not outcomes:
        print("No engine1 outcomes in window")
        return 0

    analyzer = MarketStructureAnalyzer()

    # Cross-tab: outcome_type -> agreement -> count
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_pair: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    skipped = 0

    for o in outcomes:
        candles = _load_1d_candles_before(cur, o.pair, o.fill_ts, 60)
        if len(candles) < 11:  # 2*SWING_LOOKBACK+1
            skipped += 1
            continue
        state = analyzer.analyze(candles, o.pair, "1d")
        agree = _agreement(o.direction, state.trend)
        counts[o.outcome_type][agree] += 1
        by_pair[o.pair][agree] += 1

    cur.close()
    conn.close()

    print(f"\nEngine 1 outcomes (last {args.days}d, N={len(outcomes)}, skipped={skipped} insufficient-1D-history)\n")

    print("=== Outcome × 1D bias agreement ===")
    print(f"{'outcome':<22} {'agree':>8} {'disagree':>10} {'undef':>8} {'%disagree':>11}")
    print("-" * 65)
    for oc in ["shadow_tp", "shadow_sl", "shadow_breakeven"]:
        ag = counts[oc].get("agree", 0)
        di = counts[oc].get("disagree", 0)
        un = counts[oc].get("undefined", 0)
        tot = ag + di + un
        pct = 100.0 * di / tot if tot else 0
        print(f"{oc:<22} {ag:>8} {di:>10} {un:>8} {pct:>10.1f}%")

    print()
    print("=== By pair (all outcomes) ===")
    print(f"{'pair':<14} {'agree':>8} {'disagree':>10} {'undef':>8} {'%disagree':>11}")
    print("-" * 55)
    for pair, d in sorted(by_pair.items()):
        ag = d.get("agree", 0)
        di = d.get("disagree", 0)
        un = d.get("undefined", 0)
        tot = ag + di + un
        pct = 100.0 * di / tot if tot else 0
        print(f"{pair:<14} {ag:>8} {di:>10} {un:>8} {pct:>10.1f}%")

    print()
    print("=== Decision rule check ===")
    sl_total = sum(counts["shadow_sl"].values())
    tp_total = sum(counts["shadow_tp"].values())
    sl_disagree_pct = (
        100.0 * counts["shadow_sl"].get("disagree", 0) / sl_total
        if sl_total else 0
    )
    tp_disagree_pct = (
        100.0 * counts["shadow_tp"].get("disagree", 0) / tp_total
        if tp_total else 0
    )
    print(f"SL disagree-1D rate:  {sl_disagree_pct:.1f}% (decision rule: >60% to be informative)")
    print(f"TP disagree-1D rate:  {tp_disagree_pct:.1f}% (decision rule: <30%)")

    if sl_disagree_pct > 60 and tp_disagree_pct < 30:
        verdict = "INFORMATIVE — veto would filter SL > TP"
    elif abs(sl_disagree_pct - tp_disagree_pct) < 10:
        verdict = "NOT INFORMATIVE — 1D adds no signal beyond 4H+1H"
    elif sl_disagree_pct < 60:
        verdict = "WEAK — try different cut or accept noise"
    else:
        verdict = "MIXED — SL filtering would also lose some TPs"
    print(f"\nVerdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
