#!/usr/bin/env python3
"""
BE knob comparison — Batch 1 decision tool.

Replays resolved shadows from the ml_setups table through `shared.pnl_engine`
under different BE_CONFIRM_CLOSES values and reports outcome distributions
side by side. Helps decide whether Batch 1 should:

  A) Raise BE_CONFIRM_CLOSES from 0 to 1 (wick-only touches no longer arm BE)
  B) Raise TP1_RR_RATIO from 1.0 (push TP1 further from entry)
  C) Combination

Methodology:
1. Fetch shadow setups with outcome + created_at + resolved_at
2. For each: load all confirmed candles (5m+15m+1h+4h) in window,
   ordered by CONFIRMATION time
3. Replay using pnl_engine.simulate() with knob variants
4. Aggregate outcome distribution + net PnL per variant

Caveat: candle-stream ordering is approximate until migration 17 traces
accumulate (future shadows). This tool gives directional signal for Batch 1,
not exact reproduction. Per pnl_engine replay test, baseline agreement is
~70% which is enough to compare RELATIVE change between knobs.

Usage:
    python scripts/be_knob_comparison.py --days 14
    python scripts/be_knob_comparison.py --days 30 --verbose
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

from config.settings import settings
from shared.pnl_engine import CandleSlice, Outcome, Position, compute_pnl, simulate


TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


@dataclass
class Variant:
    name: str
    be_confirm_closes: int
    tp1_rr: float  # multiplier applied to (tp1_price - entry) distance


@dataclass
class VariantStats:
    name: str
    tp: int = 0
    sl: int = 0
    breakeven: int = 0
    timeout: int = 0
    no_fill: int = 0
    pnl_sum: float = 0.0
    pnl_win_sum: float = 0.0
    pnl_loss_sum: float = 0.0

    @property
    def total_resolved(self) -> int:
        return self.tp + self.sl + self.breakeven + self.timeout

    @property
    def total(self) -> int:
        return self.total_resolved + self.no_fill

    @property
    def wr(self) -> float:
        denom = self.tp + self.sl
        return self.tp / denom if denom > 0 else 0.0

    @property
    def be_rate(self) -> float:
        return self.breakeven / self.total_resolved if self.total_resolved else 0.0

    @property
    def pf(self) -> float:
        return abs(self.pnl_win_sum / self.pnl_loss_sum) if self.pnl_loss_sum < 0 else float("inf")


def _conn():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=5,
    )
    conn.autocommit = True
    return conn


def load_candles(cur, pair: str, tfs: list[str], start_ms: int, end_ms: int) -> list[CandleSlice]:
    out: list[CandleSlice] = []
    for tf in tfs:
        tf_ms = TF_MS[tf]
        cur.execute(
            "SELECT timestamp, high, low, close FROM candles "
            "WHERE pair=%s AND timeframe=%s "
            "AND (timestamp + %s) BETWEEN %s AND %s "
            "ORDER BY timestamp",
            (pair, tf, tf_ms, start_ms, end_ms),
        )
        for row in cur.fetchall():
            confirm = int(row[0]) + tf_ms
            out.append(CandleSlice(
                high=float(row[1]), low=float(row[2]),
                close=float(row[3]), timestamp=confirm,
            ))
    out.sort(key=lambda c: c.timestamp)
    return out


def replay(
    row: tuple, candles: list[CandleSlice], variant: Variant,
) -> tuple[Outcome, float]:
    (_sid, _pair, direction, _stype, entry, sl, tp1, tp2, _outcome_db, size,
     _created_ms, _resolved_ms) = row

    # Adjust tp1 distance per variant
    adjusted_tp1 = float(tp1)
    if variant.tp1_rr != 1.0:
        dist = abs(float(tp1) - float(entry))
        if direction == "long":
            adjusted_tp1 = float(entry) + dist * variant.tp1_rr
        else:
            adjusted_tp1 = float(entry) - dist * variant.tp1_rr

    pos = Position(
        direction=direction,
        entry_price=float(entry),
        sl_price=float(sl),
        tp1_price=adjusted_tp1,
        tp2_price=float(tp2),
        position_size=float(size),
        be_confirm_closes=variant.be_confirm_closes,
    )
    outcome, pnl = simulate(pos, candles, fee_rate=settings.TRADING_FEE_RATE)
    return outcome, pnl.net_usd


def run(days: int, verbose: bool = False) -> None:
    variants = [
        Variant(name="baseline (BE=0, TP1_RR×1.0)", be_confirm_closes=0, tp1_rr=1.0),
        Variant(name="BE_CONFIRM=1 only", be_confirm_closes=1, tp1_rr=1.0),
        Variant(name="TP1_RR×1.2", be_confirm_closes=0, tp1_rr=1.2),
        Variant(name="TP1_RR×1.3", be_confirm_closes=0, tp1_rr=1.3),
        Variant(name="TP1_RR×1.4", be_confirm_closes=0, tp1_rr=1.4),
        Variant(name="TP1_RR×1.5", be_confirm_closes=0, tp1_rr=1.5),
        Variant(name="TP1_RR×1.75", be_confirm_closes=0, tp1_rr=1.75),
        Variant(name="TP1_RR×2.0 (= TP2)", be_confirm_closes=0, tp1_rr=2.0),
    ]
    stats = {v.name: VariantStats(name=v.name) for v in variants}

    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT setup_id, pair, direction, setup_type,
                   entry_price, sl_price, tp1_price, tp2_price,
                   outcome_type, shadow_position_size,
                   EXTRACT(EPOCH FROM created_at)::bigint * 1000,
                   EXTRACT(EPOCH FROM resolved_at)::bigint * 1000
            FROM ml_setups
            WHERE shadow_mode = true
              AND outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven',
                                   'shadow_timeout','shadow_no_fill')
              AND created_at > NOW() - INTERVAL '{days} days'
              AND shadow_position_size IS NOT NULL
              AND shadow_position_size > 0
            ORDER BY created_at DESC
            """
        )
        rows = cur.fetchall()
        print(f"Fetched {len(rows)} resolved shadows from last {days} days.\n")

        by_pair: dict[str, list[CandleSlice]] = {}
        for i, row in enumerate(rows):
            (_sid, pair, _dir, _stype, _e, _s, _t1, _t2, outcome_db, _size,
             created_ms, resolved_ms) = row
            start = int(created_ms) - 300_000
            end = int(resolved_ms) + 7_200_000
            candles = load_candles(cur, pair, ["5m", "15m", "1h", "4h"], start, end)
            if not candles:
                continue

            for variant in variants:
                outcome, pnl = replay(row, candles, variant)
                s = stats[variant.name]
                if outcome == Outcome.TP:
                    s.tp += 1
                elif outcome == Outcome.SL:
                    s.sl += 1
                elif outcome == Outcome.BREAKEVEN:
                    s.breakeven += 1
                elif outcome == Outcome.TIMEOUT:
                    s.timeout += 1
                elif outcome == Outcome.NO_FILL:
                    s.no_fill += 1
                s.pnl_sum += pnl
                if pnl > 0:
                    s.pnl_win_sum += pnl
                elif pnl < 0:
                    s.pnl_loss_sum += pnl

            if verbose and (i + 1) % 20 == 0:
                print(f"  processed {i+1}/{len(rows)}")

    conn.close()

    # Print report
    print("=" * 100)
    print(f"{'Variant':<38} {'Total':>6} {'TP':>4} {'SL':>4} {'BE':>4} {'TO':>4} "
          f"{'NF':>4} {'WR':>6} {'BE%':>6} {'PF':>6} {'ΣPnL':>10}")
    print("=" * 100)
    for v in variants:
        s = stats[v.name]
        print(f"{s.name:<38} {s.total:>6} {s.tp:>4} {s.sl:>4} {s.breakeven:>4} "
              f"{s.timeout:>4} {s.no_fill:>4} {s.wr*100:>5.1f}% "
              f"{s.be_rate*100:>5.1f}% "
              f"{('inf' if s.pf == float('inf') else f'{s.pf:.2f}'):>6} "
              f"${s.pnl_sum:>9.2f}")
    print("=" * 100)
    print()
    print("Legend: TP=shadow_tp, SL=shadow_sl, BE=shadow_breakeven, "
          "TO=shadow_timeout, NF=shadow_no_fill")
    print("WR = TP / (TP + SL), BE% = BE / resolved (excl no_fill), "
          "PF = Σwins / |Σlosses|, ΣPnL = net USD across all variants "
          "(approx due to candle-stream ordering — use for RELATIVE comparison only).")


def main():
    parser = argparse.ArgumentParser(description="BE knob comparison for Batch 1")
    parser.add_argument("--days", type=int, default=14,
                        help="Lookback window in days (default 14)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(args.days, args.verbose)


if __name__ == "__main__":
    main()
