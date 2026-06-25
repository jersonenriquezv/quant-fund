#!/usr/bin/env python3
"""Live falsification report for the /topdown edge — Bybit trades vs source alerts.

The /topdown edge (docs/audits/topdown-edge-expectancy-2026-05-25.md) was proven OFFLINE:
maker expectancy +0.13R on BTC/ETH, out-of-sample stable, p<0.0002. The armed live test:

    after N>=30 Bybit trades TAKEN from the edge alerts -> require live WR >= 30%
    AND realized maker expectancy > 0, else revert/kill.

Trades are linked to alerts by data_service.topdown_reconcile.find_matching_alert (strict
pair/dir/time/entry rule). Going forward the watcher sets the link automatically at open.
This script (a) backfills the link on already-closed trades, and (b) prints the falsification
scoreboard: N progress, WR, realized-R expectancy, and the go/revert verdict.

Usage:
    python scripts/reconcile_topdown_falsification.py            # report only (dry-run backfill)
    python scripts/reconcile_topdown_falsification.py --apply    # persist backfilled links, then report
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings  # noqa: E402
from data_service.topdown_reconcile import find_matching_alert  # noqa: E402

# Falsification gate (from the audit). Edits here must stay in sync with SYSTEM_BASELINE.
N_GATE = 30
WR_GATE = 0.30
EXPECTANCY_GATE_R = 0.0  # realized R must be > this


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def backfill(conn, apply: bool) -> tuple[int, int]:
    """Match unlinked closed BTC/ETH trades to topdown alerts.

    Returns (candidates_scanned, newly_matched). Writes only when apply=True.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, symbol, side, entry_price, opened_at
            FROM bybit_trade_annotations
            WHERE signal_alert_id IS NULL
              AND symbol IN ('BTCUSDT', 'ETHUSDT')
              AND entry_price IS NOT NULL
              AND opened_at IS NOT NULL
            ORDER BY opened_at
            """
        )
        rows = cur.fetchall()
        matched = 0
        for r in rows:
            # Reuse a second cursor for the alert lookup (RealDictCursor handled inside).
            with conn.cursor(cursor_factory=RealDictCursor) as mcur:
                m = find_matching_alert(
                    mcur,
                    symbol=r["symbol"],
                    side=r["side"],
                    entry_price=float(r["entry_price"]),
                    opened_at=r["opened_at"],
                )
            if not m:
                continue
            matched += 1
            print(
                f"  match annot #{r['id']} {r['symbol']} {r['side']} "
                f"-> alert #{m.alert_id} (lead {m.lead_hours:.1f}h, "
                f"Δentry {m.entry_diff_pct:.2f}%)"
                if m.entry_diff_pct is not None else
                f"  match annot #{r['id']} {r['symbol']} {r['side']} -> alert #{m.alert_id}"
            )
            if apply:
                with conn.cursor() as ucur:
                    ucur.execute(
                        """
                        UPDATE bybit_trade_annotations
                        SET signal_alert_id = %s,
                            topdown_brief_used = TRUE,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (m.alert_id, r["id"]),
                    )
        if apply:
            conn.commit()
        return len(rows), matched


def report(conn) -> None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Linked + closed trades = the falsification sample.
        cur.execute(
            """
            SELECT a.id, a.symbol, a.side, a.realized_r, a.pnl_usd, a.status,
                   s.pair, s.direction, s.rr AS planned_rr
            FROM bybit_trade_annotations a
            JOIN signal_scanner_alerts s ON s.id = a.signal_alert_id
            WHERE a.signal_alert_id IS NOT NULL
            ORDER BY a.opened_at
            """
        )
        rows = cur.fetchall()

    closed = [r for r in rows if r["status"] == "closed"]
    open_n = len(rows) - len(closed)
    with_r = [r for r in closed if r["realized_r"] is not None]

    print("\n" + "=" * 60)
    print("  /topdown LIVE FALSIFICATION — scoreboard")
    print("=" * 60)
    print(f"  Linked trades:      {len(rows)}  ({len(closed)} closed, {open_n} open)")
    print(f"  With realized R:    {len(with_r)}  (need SL set to compute R)")

    if not with_r:
        print("\n  No closed+R-resolved linked trades yet. Nothing to judge.")
        print(f"  Gate: N>={N_GATE}, WR>={WR_GATE:.0%}, mean realized R > {EXPECTANCY_GATE_R}")
        print("=" * 60 + "\n")
        return

    n = len(with_r)
    wins = sum(1 for r in with_r if r["realized_r"] > 0)
    wr = wins / n
    mean_r = sum(r["realized_r"] for r in with_r) / n
    total_usd = sum((r["pnl_usd"] or 0) for r in with_r)

    print(f"\n  N (resolved):       {n} / {N_GATE}")
    print(f"  Win rate:           {wr:.1%}  (gate >= {WR_GATE:.0%})")
    print(f"  Mean realized R:    {mean_r:+.3f}R  (gate > {EXPECTANCY_GATE_R})")
    print(f"  Net PnL:            ${total_usd:+.2f}")

    # Per pair+direction
    print("\n  By pair/direction:")
    buckets: dict[tuple[str, str], list[float]] = {}
    for r in with_r:
        buckets.setdefault((r["pair"], r["direction"]), []).append(r["realized_r"])
    for (pair, d), rs in sorted(buckets.items()):
        w = sum(1 for x in rs if x > 0) / len(rs)
        print(f"    {pair:9s} {d:5s}  n={len(rs):2d}  WR {w:4.0%}  E {sum(rs)/len(rs):+.3f}R")

    # Verdict
    print("\n  " + "-" * 56)
    if n < N_GATE:
        print(f"  VERDICT: INCONCLUSIVE — need {N_GATE - n} more resolved trades.")
        print(f"  (provisional: WR {wr:.0%}, E {mean_r:+.3f}R)")
    else:
        wr_pass = wr >= WR_GATE
        e_pass = mean_r > EXPECTANCY_GATE_R
        if wr_pass and e_pass:
            print(f"  VERDICT: ✅ EDGE HOLDS LIVE — WR {wr:.0%} & E {mean_r:+.3f}R clear the gate.")
        else:
            fails = []
            if not wr_pass:
                fails.append(f"WR {wr:.0%} < {WR_GATE:.0%}")
            if not e_pass:
                fails.append(f"E {mean_r:+.3f}R <= {EXPECTANCY_GATE_R}")
            print(f"  VERDICT: ❌ REVERT/KILL — {', '.join(fails)}.")
    print("  " + "-" * 56)
    print("  Reminder: edge ≠ profit, and live ≠ backtest. This is the live test.")
    print("=" * 60 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="persist backfilled links (default: dry-run)")
    args = ap.parse_args()

    conn = _conn()
    try:
        print(f"\nBackfilling unlinked closed BTC/ETH trades ({'APPLY' if args.apply else 'dry-run'})...")
        scanned, matched = backfill(conn, args.apply)
        print(f"  scanned {scanned} unlinked candidates, matched {matched}"
              + ("" if args.apply else "  (not written — re-run with --apply)"))
        report(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
