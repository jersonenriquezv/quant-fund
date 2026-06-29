"""Forward-gate / walk-forward edge check for legacy shadow setups.

Why this exists
---------------
The /shadow dashboard shows a CORRECT aggregate PnL/PF per setup, but an
aggregate cannot reveal *when* the edge happened. A setup whose entire profit
came from a short luck cluster looks identical, at the aggregate level, to one
with a persistent edge. This script splits the resolved shadow outcomes by time
so a decayed/front-loaded edge is visible.

It also de-biases two ways the raw shadow number is optimistic:
  1. Body-fill filter — keep only trades where the entry price fell inside the
     fill candle's [open, close] body (price genuinely traded through the level
     = a resting maker limit would fill). Wick-only touches are dropped because
     a maker fill there is not guaranteed.
  2. Walk-forward split — the 2nd half (most recent trades) is the honest
     out-of-sample read. With --cutoff, trades resolving AFTER the date are the
     pre-registered forward sample.

This is ANALYSIS ONLY. It never mutates ml_setups. Bot stays shadow-only.

Usage
-----
    python scripts/legacy_setup_forward_gate.py
    python scripts/legacy_setup_forward_gate.py --setup-type setup_b
    python scripts/legacy_setup_forward_gate.py --cutoff 2026-06-29 --min-pf 1.5

Reads POSTGRES_* from config.settings. EXPERIMENT_ID defaults to the runtime
settings value (the current parameter regime) — pass --experiment-id to override.
"""

from __future__ import annotations

import argparse
from datetime import datetime

import psycopg2

from config.settings import settings

TERMINAL_OUTCOMES = (
    "shadow_tp", "shadow_sl", "shadow_breakeven", "shadow_timeout",
)


def _connect():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _stats(pnls: list[float]) -> tuple[int, float, float, float]:
    """Return (n, win_rate_pct, profit_factor, total_pnl)."""
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = -sum(losses)
    pf = (sum(wins) / gross_loss) if gross_loss > 0 else float("inf")
    wr = (len(wins) / n * 100) if n else 0.0
    return n, wr, pf, sum(pnls)


def _fmt(pnls: list[float]) -> str:
    n, wr, pf, tot = _stats(pnls)
    pf_s = "  inf" if pf == float("inf") else f"{pf:5.2f}"
    return f"N={n:3} WR={wr:3.0f}% PF={pf_s} ${tot:+8.1f}"


def _body_fill_trades(cur, setup_type: str, experiment_id: str) -> list[tuple]:
    """Return [(created_at, pnl_usd), ...] for body-fill terminal outcomes, time-ordered.

    A body-fill = entry price inside the fill candle's [open, close]. Requires
    shadow_fill_candle_ts/tf to join back to the candles table.
    """
    cur.execute(
        f"""
        SELECT pnl_usd, entry_price, shadow_fill_candle_ts,
               shadow_fill_candle_tf, pair, created_at
        FROM ml_setups
        WHERE setup_type = %s
          AND experiment_id = %s
          AND outcome_type IN {TERMINAL_OUTCOMES}
          AND pnl_usd IS NOT NULL
          AND shadow_fill_candle_ts IS NOT NULL
        ORDER BY created_at
        """,
        (setup_type, experiment_id),
    )
    rows = cur.fetchall()
    out: list[tuple] = []
    for pnl, entry, fts, ftf, pair, created in rows:
        cur.execute(
            "SELECT open, close FROM candles "
            "WHERE pair = %s AND timeframe = %s AND timestamp = %s",
            (pair, ftf, fts),
        )
        fc = cur.fetchone()
        if not fc:
            continue
        op, cl = float(fc[0]), float(fc[1])
        if min(op, cl) <= float(entry) <= max(op, cl):
            out.append((created, float(pnl)))
    return out


def _setup_types(cur, experiment_id: str) -> list[str]:
    cur.execute(
        f"""
        SELECT DISTINCT setup_type FROM ml_setups
        WHERE experiment_id = %s
          AND outcome_type IN {TERMINAL_OUTCOMES}
          AND pnl_usd IS NOT NULL
        ORDER BY setup_type
        """,
        (experiment_id,),
    )
    return [r[0] for r in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--setup-type", default=None,
        help="Single setup_type (default: all in the experiment).",
    )
    ap.add_argument(
        "--experiment-id", default=settings.EXPERIMENT_ID,
        help=f"Parameter regime (default: {settings.EXPERIMENT_ID}).",
    )
    ap.add_argument(
        "--cutoff", default=None,
        help="ISO date (YYYY-MM-DD). When set, also report pre/post-cutoff "
             "split — the post sample is the pre-registered forward read.",
    )
    ap.add_argument(
        "--min-n", type=int, default=6,
        help="Skip setups with fewer than this many body-fills (default 6).",
    )
    ap.add_argument(
        "--min-pf", type=float, default=1.5,
        help="Pass bar for the forward read (default 1.5). Annotates PASS/FAIL.",
    )
    args = ap.parse_args()

    cutoff_dt = None
    if args.cutoff:
        cutoff_dt = datetime.fromisoformat(args.cutoff)

    conn = _connect()
    try:
        cur = conn.cursor()
        types = [args.setup_type] if args.setup_type else _setup_types(cur, args.experiment_id)

        print(f"Walk-forward (body-fill only) — experiment {args.experiment_id}")
        if cutoff_dt:
            print(f"Forward cutoff: {args.cutoff} (post = pre-registered forward sample)")
        print(f"Pass bar: PF >= {args.min_pf}\n")

        header = f"{'setup':22} | {'FULL':30} | {'1st half':30} | {'2nd half (forward read)':30}"
        print(header)
        print("-" * len(header))

        for st in types:
            body = _body_fill_trades(cur, st, args.experiment_id)
            if len(body) < args.min_n:
                print(f"{st:22} | N={len(body)} (< min-n {args.min_n}, skipped)")
                continue
            pnls = [p for _, p in body]
            half = len(body) // 2
            h1 = pnls[:half]
            h2 = pnls[half:]
            _, _, pf2, _ = _stats(h2)
            verdict = "PASS" if pf2 >= args.min_pf else "FAIL"
            print(
                f"{st:22} | {_fmt(pnls):30} | {_fmt(h1):30} | {_fmt(h2):30} [{verdict}]"
            )

            if cutoff_dt:
                pre = [p for c, p in body if c < cutoff_dt]
                post = [p for c, p in body if c >= cutoff_dt]
                _, _, pf_post, _ = _stats(post) if post else (0, 0, 0, 0)
                pv = (
                    "n/a" if not post
                    else ("PASS" if pf_post >= args.min_pf else "FAIL")
                )
                print(
                    f"{'  └ cutoff split':22} | "
                    f"pre  {_fmt(pre):24} | post {_fmt(post):24} [{pv}]"
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
