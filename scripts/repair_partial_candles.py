"""Repair partial (forming) candles frozen in PostgreSQL.

Root cause (see docs/grill/partial-candle-backfill-fix-2026-06-15.md): the bot's
backfill stored the currently-forming bar as ``confirmed=True`` and the candle
upsert used ``ON CONFLICT DO NOTHING``, so the later authoritative WS bar was
dropped. Result: partial bars (range strictly inside the real bar) frozen in PG.

This script re-fetches the last N **closed** bars from OKX REST (the
``history-candles`` endpoint returns only ``confirm == "1"`` rows once paginated,
so the still-forming bar is inherently excluded) and overwrites the stored bars
via ``store_candles(upsert=True)`` (``ON CONFLICT DO UPDATE``). Idempotent and
re-runnable. Reads/writes PG only — no live-bot interaction, no orders.

Run:
  source venv/bin/activate && PYTHONPATH=. \
    python scripts/repair_partial_candles.py --pair ETH/USDT --tf 4h --count 400
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import requests

from data_service.data_store import PostgresStore
from data_service.metadata import OKX_SWAP_INSTRUMENTS
from shared.models import Candle

OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
# OKX bar codes. 4h/1h boundaries are identical UTC vs HK (whole-hour offset),
# so plain "4H"/"1H" are safe here. (6h would need "6Hutc" — not repaired here.)
TF_BAR = {"4h": "4H", "1h": "1H", "15m": "15m", "5m": "5m"}


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def fetch_closed_candles(pair: str, tf: str, count: int) -> list[Candle]:
    """Fetch the most recent ``count`` CLOSED OKX bars as Candle objects.

    Paginates ``history-candles`` (100/req) backwards. Keeps only ``confirm=="1"``
    rows (closed). Volume mirrors the WS path: base ccy (col 6) + quote (col 7).
    """
    inst = OKX_SWAP_INSTRUMENTS[pair]
    bar = TF_BAR[tf]
    rows: list[list] = []
    after = ""
    while len(rows) < count:
        params = {"instId": inst, "bar": bar, "limit": "100"}
        if after:
            params["after"] = after
        data = requests.get(OKX_HISTORY_URL, params=params, timeout=15).json()
        batch = data.get("data", [])
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1][0]
        if len(batch) < 100:
            break
        time.sleep(0.1)  # stay well under OKX rate limit

    candles: list[Candle] = []
    seen: set[int] = set()
    for r in rows:
        if r[8] != "1":  # confirm flag: skip any forming bar
            continue
        ts = int(r[0])
        if ts in seen:
            continue
        seen.add(ts)
        candles.append(Candle(
            timestamp=ts,
            open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
            volume=float(r[6]) if r[6] else float(r[5]),
            volume_quote=float(r[7]) if r[7] else 0.0,
            pair=pair, timeframe=tf, confirmed=True,
        ))
    candles.sort(key=lambda c: c.timestamp)
    return candles


def repair(pair: str, tf: str, count: int) -> int:
    store = PostgresStore()
    if not store.connect():
        print("FATAL: cannot connect to PostgreSQL")
        sys.exit(2)

    fresh = fetch_closed_candles(pair, tf, count)
    if not fresh:
        print(f"FATAL: no closed bars fetched for {pair} {tf}")
        sys.exit(2)

    # Diff against what's currently stored, for a before/after report.
    existing = {c.timestamp: c for c in store.load_candles(pair, tf, count=count + 50)}
    dirty = [c for c in fresh
             if c.timestamp in existing and (
                 abs(existing[c.timestamp].high - c.high) > 1e-6 or
                 abs(existing[c.timestamp].low - c.low) > 1e-6 or
                 abs(existing[c.timestamp].close - c.close) > 1e-6)]
    new_bars = [c for c in fresh if c.timestamp not in existing]

    print(f"--- REPAIR {pair} {tf} "
          f"({_fmt(fresh[0].timestamp)} -> {_fmt(fresh[-1].timestamp)}) ---")
    print(f"  fetched closed bars : {len(fresh)}")
    print(f"  already stored      : {len(existing)}")
    print(f"  partial/dirty (will overwrite): {len(dirty)}")
    print(f"  missing (will insert)         : {len(new_bars)}")
    if dirty:
        print("  sample dirty bars (stored -> REST):")
        for c in dirty[:5]:
            e = existing[c.timestamp]
            print(f"    {_fmt(c.timestamp)} "
                  f"H {e.high:.2f}->{c.high:.2f}  L {e.low:.2f}->{c.low:.2f}  "
                  f"C {e.close:.2f}->{c.close:.2f}")

    written = store.store_candles(fresh, upsert=True)
    print(f"  upsert rowcount     : {written}")
    print(f"  DONE — re-run the parity tracer to confirm 0 mismatches.")
    return len(dirty)


def main() -> None:
    ap = argparse.ArgumentParser(description="Repair partial candles in PostgreSQL.")
    ap.add_argument("--pair", default="ETH/USDT")
    ap.add_argument("--tf", default="4h", choices=list(TF_BAR))
    ap.add_argument("--count", type=int, default=400)
    args = ap.parse_args()
    repair(args.pair, args.tf, args.count)


if __name__ == "__main__":
    main()
