"""Backfill annotations table from existing bybit_closed_pnl rows.

For trades that closed before the watcher was running. Creates one annotation
per closed PnL row, marked closed, with pnl data populated. No context snapshot
(not available retroactively). User can fill setup_type/thesis manually.
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


def backfill(since_days: int) -> int:
    """Create annotation rows from bybit_closed_pnl. Uses created_time as opened_at."""
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT p.* FROM bybit_closed_pnl p
            WHERE p.created_time >= NOW() - (%s * INTERVAL '1 day')
              AND NOT EXISTS (
                SELECT 1 FROM bybit_trade_annotations a
                WHERE a.closed_pnl_id = p.id
              )
            ORDER BY p.created_time ASC
            """,
            (since_days,),
        )
        rows = cur.fetchall()

        inserted = 0
        for r in rows:
            qty = float(r.get("qty") or 0)
            entry = float(r.get("avg_entry_price") or 0)
            notional = qty * entry
            pnl = float(r.get("closed_pnl") or 0)
            cum_entry = float(r.get("cum_entry_value") or 0)
            pnl_pct = (pnl / cum_entry * 100) if cum_entry else None
            # r['side'] = exit side. Invert to get opened side.
            exit_side = r.get("side")
            open_side = "Buy" if exit_side == "Sell" else "Sell"
            try:
                cur.execute(
                    """
                    INSERT INTO bybit_trade_annotations (
                        symbol, side, opened_at, entry_price, size, leverage,
                        notional_value, closed_at, exit_price, pnl_usd, pnl_pct,
                        closed_pnl_id, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'closed')
                    ON CONFLICT (symbol, side, opened_at) DO NOTHING
                    """,
                    (
                        r["symbol"], open_side, r["created_time"],
                        entry, qty, r.get("leverage"),
                        notional, r["updated_time"], r.get("avg_exit_price"),
                        pnl, pnl_pct, r["id"],
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
            except psycopg2.errors.UniqueViolation:
                c.rollback()
                continue
        c.commit()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    n = backfill(args.days)
    print(f"backfilled {n} annotation rows from bybit_closed_pnl (last {args.days}d)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
