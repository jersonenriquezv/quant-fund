"""Liquidation cascade shadow detector + historical scanner.

Structural-edge candidate that survived a grill (PIVOT verdict):
docs/grill/liquidation-cascade-reversion-2026-05-25.md.

Thesis: a forced-liquidation cascade overshoots fair value; when it exhausts,
price mean-reverts. The victim is the liquidated leverage. Cascade SIZE
discriminates: medium flushes (1.5-2.5%) CONTINUE (trend acceleration), large
flushes (>2.5%) REVERT hard (capitulation). The large-bucket edge is real but
data-starved (~10 events / 106d / 4 pairs) — this tool accumulates N toward a
go/no-go at N>=30.

Cascade = OI drop >= OI_DROP_PCT over <= WIN_MS, with a concurrent price move
>= MIN_PRICE_MOVE. Liquidation side inferred from price direction during the
flush (price down => longs liquidated => mean-revert UP).

Modes:
  --scan           Historical scan over all stored OI+candle data → table + CSV + report.
  --report         Re-print the size-bucket reliability report from the table.

Run:
  PYTHONPATH=. venv/bin/python scripts/cascade_shadow.py --scan
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from datetime import datetime, timezone
from typing import Optional

import psycopg2

from config.settings import settings


# Detection parameters (validated in the grill)
OI_DROP_PCT = 0.02        # OI must drop >= 2% over the window
WIN_MS = 15 * 60 * 1000   # cascade window: 15 minutes
MIN_PRICE_MOVE = 0.01     # >= 1% concurrent price move = cascade (not drift)
DEDUP_MS = 60 * 60 * 1000  # 1h dedup between events on the same pair
FWD_MINUTES = [30, 60, 120]  # forward horizons for mean-reversion measurement
RECLAIM_BARS = 2          # bars after flush to check for a reclaim of pre-flush level

DEFAULT_PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT",
    "XRP/USDT", "LINK/USDT", "AVAX/USDT",
]

SIZE_BUCKETS = [
    ("1-1.5%", 0.010, 0.015),
    ("1.5-2.5%", 0.015, 0.025),
    ("2.5-4%", 0.025, 0.040),
    ("4%+", 0.040, 99.0),
]


def _connect():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _size_bucket(abs_move: float) -> str:
    for name, lo, hi in SIZE_BUCKETS:
        if lo <= abs_move < hi:
            return name
    return "sub-1%"


def ensure_cascades_table() -> None:
    """Create liquidation_cascades if missing. Self-contained (script owns it)."""
    sql = """
    CREATE TABLE IF NOT EXISTS liquidation_cascades (
        id BIGSERIAL PRIMARY KEY,
        pair VARCHAR(20) NOT NULL,
        cascade_ts BIGINT NOT NULL,
        oi_drop_pct REAL,
        price_move_pct REAL,
        size_bucket VARCHAR(12),
        liq_side VARCHAR(12),
        entry_price REAL,
        reclaim BOOLEAN,
        fwd_ret_30m REAL,
        fwd_ret_60m REAL,
        fwd_ret_120m REAL,
        source VARCHAR(20) DEFAULT 'historical_scan',
        detected_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(pair, cascade_ts)
    );
    CREATE INDEX IF NOT EXISTS idx_cascades_pair_ts
        ON liquidation_cascades(pair, cascade_ts DESC);
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
    finally:
        conn.close()


def _load_oi(cur, pair: str) -> list[tuple[int, float]]:
    cur.execute(
        "SELECT timestamp, oi_base FROM open_interest_history "
        "WHERE pair = %s ORDER BY timestamp",
        (pair,),
    )
    return [(int(r[0]), float(r[1])) for r in cur.fetchall()]


def _load_5m(cur, pair: str) -> list[tuple[int, float, float, float, float]]:
    cur.execute(
        "SELECT timestamp, open, high, low, close FROM candles "
        "WHERE pair = %s AND timeframe = '5m' ORDER BY timestamp",
        (pair,),
    )
    return [
        (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in cur.fetchall()
    ]


def _candle_at(candles: list, ts: int):
    """Nearest 5m candle at or before ts (binary search)."""
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid][0] <= ts:
            lo = mid + 1
        else:
            hi = mid
    return candles[lo - 1] if lo > 0 else None


def detect_cascades(pair: str, oi: list, candles: list) -> list[dict]:
    """Detect cascade events for one pair. Returns list of event dicts with
    forward mean-reversion returns filled where candle data permits."""
    events: list[dict] = []
    if not oi or not candles:
        return events
    last_event_ts = 0
    for j in range(len(oi)):
        ts_j, oi_j = oi[j]
        # Walk back to the sample ~WIN_MS earlier
        k = j
        while k > 0 and ts_j - oi[k][0] < WIN_MS:
            k -= 1
        if k == j or oi[k][1] <= 0:
            continue
        oi_prev = oi[k][1]
        drop = (oi_prev - oi_j) / oi_prev
        if drop < OI_DROP_PCT:
            continue
        if ts_j - last_event_ts < DEDUP_MS:
            continue
        p_start = _candle_at(candles, oi[k][0])
        p_end = _candle_at(candles, ts_j)
        if not p_start or not p_end:
            continue
        price_move = (p_end[4] - p_start[4]) / p_start[4]
        if abs(price_move) < MIN_PRICE_MOVE:
            continue
        last_event_ts = ts_j

        liq_side = "long_liq" if price_move < 0 else "short_liq"
        entry = p_end[4]

        # Reclaim flag: within RECLAIM_BARS, does price close back toward the
        # pre-flush level (recovering part of the overshoot)?
        reclaim = False
        for b in range(1, RECLAIM_BARS + 1):
            pc = _candle_at(candles, ts_j + b * 5 * 60 * 1000)
            if not pc:
                break
            if liq_side == "long_liq" and pc[4] > entry:
                reclaim = True
                break
            if liq_side == "short_liq" and pc[4] < entry:
                reclaim = True
                break

        fwd = {}
        for fm in FWD_MINUTES:
            pf = _candle_at(candles, ts_j + fm * 60 * 1000)
            if not pf:
                fwd[fm] = None
                continue
            raw = (pf[4] - entry) / entry
            fwd[fm] = raw if liq_side == "long_liq" else -raw  # mean-reversion direction

        events.append({
            "pair": pair,
            "cascade_ts": ts_j,
            "oi_drop_pct": round(drop * 100, 3),
            "price_move_pct": round(price_move * 100, 3),
            "size_bucket": _size_bucket(abs(price_move)),
            "liq_side": liq_side,
            "entry_price": entry,
            "reclaim": reclaim,
            "fwd_ret_30m": fwd.get(30),
            "fwd_ret_60m": fwd.get(60),
            "fwd_ret_120m": fwd.get(120),
        })
    return events


def _upsert_events(events: list[dict]) -> int:
    if not events:
        return 0
    conn = _connect()
    inserted = 0
    try:
        with conn.cursor() as cur:
            for e in events:
                cur.execute(
                    """
                    INSERT INTO liquidation_cascades
                      (pair, cascade_ts, oi_drop_pct, price_move_pct, size_bucket,
                       liq_side, entry_price, reclaim, fwd_ret_30m, fwd_ret_60m,
                       fwd_ret_120m, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'historical_scan')
                    ON CONFLICT (pair, cascade_ts) DO UPDATE SET
                      fwd_ret_30m = EXCLUDED.fwd_ret_30m,
                      fwd_ret_60m = EXCLUDED.fwd_ret_60m,
                      fwd_ret_120m = EXCLUDED.fwd_ret_120m,
                      reclaim = EXCLUDED.reclaim
                    """,
                    (
                        e["pair"], e["cascade_ts"], e["oi_drop_pct"],
                        e["price_move_pct"], e["size_bucket"], e["liq_side"],
                        e["entry_price"], e["reclaim"], e["fwd_ret_30m"],
                        e["fwd_ret_60m"], e["fwd_ret_120m"],
                    ),
                )
                inserted += 1
            conn.commit()
    finally:
        conn.close()
    return inserted


def _bucket_report(events: list[dict]) -> str:
    """Size-bucket mean-reversion reliability report (markdown)."""
    lines = []
    lines.append(f"# Liquidation Cascade Shadow — historical scan")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Params:** OI drop >= {OI_DROP_PCT*100:.0f}% / {WIN_MS//60000}min, "
                 f"price move >= {MIN_PRICE_MOVE*100:.0f}%, {DEDUP_MS//3600000}h dedup")
    lines.append(f"**Total cascades:** {len(events)}")
    long_liq = sum(1 for e in events if e["liq_side"] == "long_liq")
    lines.append(f"  long_liq (price fell): {long_liq} | short_liq (price rose): {len(events)-long_liq}")
    lines.append("")
    lines.append("## Mean-reversion forward return by cascade size")
    lines.append("")
    lines.append("`net` = mean MR return − 0.11% (taker round-trip). MR = bounce in the")
    lines.append("liquidation-reversal direction (long_liq → up, short_liq → down).")
    lines.append("")
    lines.append("| Size bucket | N | mean MR 60m | %pos 60m | net taker | N reclaim |")
    lines.append("|---|---|---|---|---|---|")
    for name, lo, hi in SIZE_BUCKETS:
        sub = [e for e in events if e["size_bucket"] == name]
        rets = [e["fwd_ret_60m"] for e in sub if e["fwd_ret_60m"] is not None]
        if not rets:
            lines.append(f"| {name} | 0 | — | — | — | 0 |")
            continue
        mean = statistics.mean(rets) * 100
        pos = sum(1 for r in rets if r > 0) / len(rets) * 100
        n_reclaim = sum(1 for e in sub if e["reclaim"])
        lines.append(
            f"| {name} | {len(sub)} | {mean:+.3f}% | {pos:.1f}% | "
            f"{mean-0.11:+.3f}% | {n_reclaim} |"
        )
    lines.append("")
    lines.append("## Decision gate")
    lines.append("")
    large = [e for e in events if e["size_bucket"] in ("2.5-4%", "4%+")
             and e["fwd_ret_60m"] is not None]
    n_large = len(large)
    lines.append(f"- Large-cascade (>2.5%) count: **{n_large}** (need N>=30 for go/no-go).")
    if n_large >= 30:
        mr = statistics.mean(e["fwd_ret_60m"] for e in large) * 100
        pos = sum(1 for e in large if e["fwd_ret_60m"] > 0) / n_large * 100
        lines.append(f"- Large-cascade MR 60m: {mr:+.3f}%, {pos:.1f}% pos. "
                     f"{'PROMOTE candidate' if mr-0.11 > 0 and pos > 60 else 'edge regressed — KILL'}.")
    else:
        lines.append(f"- **{30-n_large} more large cascades needed.** Keep accumulating "
                     f"(live detector or future scans as data grows).")
    lines.append("")
    lines.append("Source grill: `docs/grill/liquidation-cascade-reversion-2026-05-25.md`")
    lines.append("")
    return "\n".join(lines)


def run_scan(pairs: list[str]) -> int:
    ensure_cascades_table()
    conn = _connect()
    cur = conn.cursor()
    all_events: list[dict] = []
    try:
        for pair in pairs:
            oi = _load_oi(cur, pair)
            candles = _load_5m(cur, pair)
            ev = detect_cascades(pair, oi, candles)
            print(f"[{pair}] OI={len(oi)} candles={len(candles)} → {len(ev)} cascades")
            all_events.extend(ev)
    finally:
        cur.close()
        conn.close()

    inserted = _upsert_events(all_events)
    print(f"\nUpserted {inserted} cascade events into liquidation_cascades")

    out_dir = "backtest_results"
    os.makedirs(out_dir, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("cascade_%Y%m%d_%H%M%S")
    csv_path = os.path.join(out_dir, f"{run_id}_events.csv")
    if all_events:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_events[0].keys()))
            w.writeheader()
            w.writerows(all_events)
        print(f"Wrote {csv_path}")

    report = _bucket_report(all_events)
    report_path = os.path.join(out_dir, f"{run_id}_report.md")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Wrote {report_path}\n")
    print(report)
    return 0


def run_report() -> int:
    """Re-print the size-bucket report from the table."""
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT pair, cascade_ts, oi_drop_pct, price_move_pct, size_bucket, "
            "liq_side, entry_price, reclaim, fwd_ret_30m, fwd_ret_60m, fwd_ret_120m "
            "FROM liquidation_cascades ORDER BY cascade_ts"
        )
        cols = ["pair", "cascade_ts", "oi_drop_pct", "price_move_pct", "size_bucket",
                "liq_side", "entry_price", "reclaim", "fwd_ret_30m", "fwd_ret_60m",
                "fwd_ret_120m"]
        events = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    if not events:
        print("No cascade events in table. Run --scan first.")
        return 1
    print(_bucket_report(events))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Liquidation cascade shadow detector")
    parser.add_argument("--scan", action="store_true",
                        help="Historical scan over stored OI+candles → table + CSV + report")
    parser.add_argument("--report", action="store_true",
                        help="Re-print size-bucket report from the table")
    parser.add_argument("--pairs", type=str, default=None,
                        help="Comma-separated pair override (default: all 7)")
    args = parser.parse_args()

    pairs = ([p.strip() for p in args.pairs.split(",") if p.strip()]
             if args.pairs else DEFAULT_PAIRS)

    if args.scan:
        return run_scan(pairs)
    if args.report:
        return run_report()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
