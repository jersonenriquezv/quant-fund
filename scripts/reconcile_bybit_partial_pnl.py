"""Reconcile annotation pnl_usd against summed closed_pnl rows.

Repairs the partial-close undercount bug: pre-fix `_close_annotation` only
read the most recent closed_pnl row, so trades closed via multiple limit
fills have annotation.pnl_usd reflecting only the final partial. This
script recomputes pnl_usd / pnl_pct / exit_price across the full position
lifecycle and updates rows where the delta is material.

Usage:
    python scripts/reconcile_bybit_partial_pnl.py --days 30 --dry-run
    python scripts/reconcile_bybit_partial_pnl.py --days 30 --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor

from config.settings import settings


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def reconcile(days: int, apply: bool, tolerance: float = 0.01) -> int:
    """Walk closed annotations, recompute aggregated PnL, report or update.

    Args:
        days: only inspect annotations closed in the last N days.
        apply: when True, UPDATE rows where |delta| > tolerance USD.
        tolerance: ignore updates smaller than this (avoids float noise).

    Returns:
        Number of rows that needed (or got) updated.
    """
    affected = 0
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, symbol, side, opened_at, closed_at, pnl_usd, pnl_pct, exit_price
            FROM bybit_trade_annotations
            WHERE status = 'closed'
              AND closed_at >= NOW() - (%s * INTERVAL '1 day')
            ORDER BY closed_at DESC
            """,
            (days,),
        )
        annotations = cur.fetchall()

        print(f"Inspecting {len(annotations)} closed annotations from last {days}d\n")
        print(f"{'id':>6}  {'symbol':<10} {'side':<5} "
              f"{'stored_pnl':>11} {'agg_pnl':>11} {'delta':>9} {'partials':>9}")
        print("-" * 75)

        for a in annotations:
            cur.execute(
                """
                SELECT
                    SUM(closed_pnl)                          AS total_pnl,
                    SUM(cum_entry_value)                     AS total_entry_value,
                    SUM(qty * avg_exit_price)
                        / NULLIF(SUM(qty), 0)                AS weighted_exit_price,
                    COUNT(*)                                 AS rows_counted,
                    (array_agg(id ORDER BY updated_time DESC))[1] AS last_id
                FROM bybit_closed_pnl
                WHERE symbol = %s
                  AND updated_time >= %s - INTERVAL '1 minute'
                  AND updated_time <= %s + INTERVAL '5 minutes'
                """,
                (a["symbol"], a["opened_at"], a["closed_at"]),
            )
            agg = cur.fetchone()
            n = int(agg["rows_counted"] or 0)
            if n == 0:
                continue
            new_pnl = float(agg["total_pnl"] or 0.0)
            stored_pnl = float(a["pnl_usd"] or 0.0)
            delta = new_pnl - stored_pnl

            if abs(delta) <= tolerance:
                continue

            new_exit = float(agg["weighted_exit_price"]) if agg["weighted_exit_price"] else None
            new_entry_value = float(agg["total_entry_value"]) if agg["total_entry_value"] else None
            new_pnl_pct = 100.0 * new_pnl / new_entry_value if new_entry_value else None
            last_id = agg["last_id"]

            print(f"{a['id']:>6}  {a['symbol']:<10} {a['side']:<5} "
                  f"{stored_pnl:>11.4f} {new_pnl:>11.4f} {delta:>+9.4f} {n:>9}")

            affected += 1
            if apply:
                cur.execute(
                    """
                    UPDATE bybit_trade_annotations
                    SET pnl_usd = %s,
                        pnl_pct = %s,
                        exit_price = %s,
                        closed_pnl_id = COALESCE(%s, closed_pnl_id),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (new_pnl, new_pnl_pct, new_exit, last_id, a["id"]),
                )

        if apply:
            c.commit()

    mode = "updated" if apply else "would update"
    print(f"\n{mode} {affected} annotations (|delta| > ${tolerance:.2f})")
    return affected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="Lookback window (days)")
    ap.add_argument("--apply", action="store_true", help="Persist updates (default: dry-run)")
    ap.add_argument("--tolerance", type=float, default=0.01, help="Skip deltas below this USD")
    args = ap.parse_args()
    reconcile(days=args.days, apply=args.apply, tolerance=args.tolerance)


if __name__ == "__main__":
    main()
