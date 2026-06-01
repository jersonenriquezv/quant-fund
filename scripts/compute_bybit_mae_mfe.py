"""Bybit journal v2 — MAE/MFE + R-metric batch backfill (Phase 4).

For every closed v2 annotation, fetch 1m candles spanning the trade window
(opened_at -> closed_at) via Bybit REST, then compute direction-aware excursions
and R metrics:

    R_price = |entry - sl|              (entry/sl prefer planned_*, fall back to actual)
    R_usd   = R_price * size
    mfe_r   = max favorable excursion / R_price   (>= 0)
    mae_r   = -(worst adverse excursion) / R_price (<= 0)
    realized_r      = closed_pnl / R_usd          (pnl_usd is already net of fees —
                                                   do NOT re-deduct, see memory
                                                   feedback_pnl_already_net_of_fees)
    exit_efficiency = realized_r / mfe_r          (NULL when mfe_r <= 0)
    entry_slippage_bps = adverse fill vs planned entry (NULL without planned entry)

1m candles are NOT stored by the bot (only 5m/15m/1h/4h), so they are fetched
on demand and discarded — nothing persisted. Re-runnable + idempotent: only rows
with NULL mae_r are processed unless --force. Nightly-friendly.

Run: PYTHONPATH=. python scripts/compute_bybit_mae_mfe.py
     PYTHONPATH=. python scripts/compute_bybit_mae_mfe.py --days 30 --force
     PYTHONPATH=. python scripts/compute_bybit_mae_mfe.py --dry-run --limit 5
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional

import psycopg2

from config.settings import settings
from data_service.context_service import bybit_symbol_to_pair  # noqa: F401  (parity / symbol docs)

# Bybit kline limit per request.
_KLINE_LIMIT = 1000
_MIN_MS = 60_000


@dataclass
class TradeRow:
    annot_id: int
    symbol: str
    side: str            # "Buy" / "Sell"
    entry_price: Optional[float]
    size: Optional[float]
    closed_pnl: Optional[float]
    position_sl: Optional[float]
    planned_entry: Optional[float]
    planned_sl: Optional[float]
    opened_ms: int
    closed_ms: int

    @property
    def direction(self) -> str:
        return "long" if self.side == "Buy" else "short"


@dataclass
class Metrics:
    mae_r: float
    mfe_r: float
    realized_r: Optional[float]
    exit_efficiency: Optional[float]
    entry_slippage_bps: Optional[float]
    candle_count: int


def _connect():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _make_client():
    from pybit.unified_trading import HTTP
    if not settings.BYBIT_API_KEY or not settings.BYBIT_API_SECRET:
        raise RuntimeError("BYBIT_API_KEY / BYBIT_API_SECRET missing")
    return HTTP(
        testnet=settings.BYBIT_TESTNET,
        api_key=settings.BYBIT_API_KEY,
        api_secret=settings.BYBIT_API_SECRET,
    )


def pull_rows(cur, days: int, limit: int, force: bool) -> list[TradeRow]:
    cur.execute(
        """
        SELECT id, symbol, side, entry_price, size, pnl_usd,
               position_sl_price, planned_entry_price, planned_sl_price,
               EXTRACT(EPOCH FROM opened_at) * 1000,
               EXTRACT(EPOCH FROM closed_at) * 1000
        FROM bybit_trade_annotations
        WHERE status = 'closed'
          AND journal_schema_version = 2
          AND opened_at IS NOT NULL AND closed_at IS NOT NULL
          AND closed_at >= NOW() - (%s || ' days')::interval
          AND (%s OR mae_r IS NULL)
        ORDER BY closed_at DESC
        LIMIT %s
        """,
        (days, force, limit),
    )
    rows: list[TradeRow] = []
    for r in cur.fetchall():
        rows.append(TradeRow(
            annot_id=int(r[0]),
            symbol=r[1],
            side=r[2],
            entry_price=float(r[3]) if r[3] is not None else None,
            size=float(r[4]) if r[4] is not None else None,
            closed_pnl=float(r[5]) if r[5] is not None else None,
            position_sl=float(r[6]) if r[6] is not None else None,
            planned_entry=float(r[7]) if r[7] is not None else None,
            planned_sl=float(r[8]) if r[8] is not None else None,
            opened_ms=int(r[9]),
            closed_ms=int(r[10]),
        ))
    return rows


def fetch_1m_candles(client, symbol: str, start_ms: int, end_ms: int) -> list[tuple[float, float]]:
    """Return [(high, low)] for 1m candles in [start_ms, end_ms]. Paginated.

    Bybit returns newest-first; order is irrelevant for max/min so we just collect.
    """
    out: list[tuple[float, float]] = []
    cur_end = end_ms
    seen: set[int] = set()
    while cur_end >= start_ms:
        resp = client.get_kline(
            category="linear", symbol=symbol, interval="1",
            start=start_ms, end=cur_end, limit=_KLINE_LIMIT,
        )
        lst = (resp.get("result") or {}).get("list", []) or []
        if not lst:
            break
        oldest = None
        for k in lst:
            try:
                ts = int(k[0])
                if ts in seen:
                    continue
                seen.add(ts)
                out.append((float(k[2]), float(k[3])))  # high, low
                oldest = ts if oldest is None else min(oldest, ts)
            except (TypeError, ValueError, IndexError):
                continue
        if oldest is None or oldest <= start_ms or len(lst) < _KLINE_LIMIT:
            break
        cur_end = oldest - 1
    return out


def compute_metrics(row: TradeRow, candles: list[tuple[float, float]]) -> Optional[Metrics]:
    """Direction-aware excursions + R metrics. None when the R unit has no source."""
    entry = row.planned_entry if row.planned_entry is not None else row.entry_price
    sl = row.planned_sl if row.planned_sl is not None else row.position_sl
    if entry is None or sl is None or entry == sl or not row.size or not candles:
        return None
    r_price = abs(entry - sl)
    r_usd = r_price * abs(row.size)
    if r_price <= 0 or r_usd <= 0:
        return None

    highs = [c[0] for c in candles]
    lows = [c[1] for c in candles]
    if row.direction == "long":
        mfe_abs = max(0.0, max(highs) - entry)
        mae_abs = max(0.0, entry - min(lows))
    else:
        mfe_abs = max(0.0, entry - min(lows))
        mae_abs = max(0.0, max(highs) - entry)

    mfe_r = mfe_abs / r_price
    mae_r = -(mae_abs / r_price)  # schema: mae_r <= 0

    realized_r = row.closed_pnl / r_usd if row.closed_pnl is not None else None
    exit_eff = realized_r / mfe_r if (realized_r is not None and mfe_r > 0) else None

    slippage = None
    if row.planned_entry and row.entry_price and row.planned_entry > 0:
        raw_bps = (row.entry_price - row.planned_entry) / row.planned_entry * 10_000
        slippage = raw_bps if row.direction == "long" else -raw_bps  # positive = worse fill

    return Metrics(
        mae_r=round(mae_r, 4),
        mfe_r=round(mfe_r, 4),
        realized_r=round(realized_r, 4) if realized_r is not None else None,
        exit_efficiency=round(exit_eff, 4) if exit_eff is not None else None,
        entry_slippage_bps=round(slippage, 2) if slippage is not None else None,
        candle_count=len(candles),
    )


def write_metrics(cur, annot_id: int, m: Metrics) -> None:
    cur.execute(
        """
        UPDATE bybit_trade_annotations
        SET mae_r = %s, mfe_r = %s, realized_r = %s, exit_efficiency = %s,
            entry_slippage_bps = %s, mae_mfe_tf = '1m', updated_at = NOW()
        WHERE id = %s
        """,
        (m.mae_r, m.mfe_r, m.realized_r, m.exit_efficiency,
         m.entry_slippage_bps, annot_id),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90, help="lookback window on closed_at")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--force", action="store_true", help="recompute rows that already have mae_r")
    parser.add_argument("--dry-run", action="store_true", help="compute + print, do not write")
    args = parser.parse_args()

    conn = _connect()
    cur = conn.cursor()
    rows = pull_rows(cur, args.days, args.limit, args.force)
    if not rows:
        print("No closed v2 rows to backfill.")
        cur.close()
        conn.close()
        return 0

    client = _make_client()
    print(f"{'id':>5} {'symbol':<10} {'dir':<5} {'mfe_r':>7} {'mae_r':>7} "
          f"{'real_r':>7} {'exit_eff':>8} {'slip_bps':>8} {'cnd':>4}")
    print("-" * 72)

    done = skipped = 0
    for row in rows:
        try:
            candles = fetch_1m_candles(
                client, row.symbol, row.opened_ms - _MIN_MS, row.closed_ms + _MIN_MS
            )
        except Exception as exc:
            print(f"{row.annot_id:>5} {row.symbol:<10} kline fetch failed: {exc}")
            skipped += 1
            continue
        m = compute_metrics(row, candles)
        if m is None:
            print(f"{row.annot_id:>5} {row.symbol:<10} {row.direction:<5}  "
                  f"skipped (no R source / no candles)")
            skipped += 1
            continue
        re_s = f"{m.realized_r:.2f}" if m.realized_r is not None else "N/A"
        ee_s = f"{m.exit_efficiency:.2f}" if m.exit_efficiency is not None else "N/A"
        sl_s = f"{m.entry_slippage_bps:.1f}" if m.entry_slippage_bps is not None else "N/A"
        print(f"{row.annot_id:>5} {row.symbol:<10} {row.direction:<5} "
              f"{m.mfe_r:>7.2f} {m.mae_r:>7.2f} {re_s:>7} {ee_s:>8} {sl_s:>8} {m.candle_count:>4}")
        if not args.dry_run:
            write_metrics(cur, row.annot_id, m)
        done += 1

    if not args.dry_run:
        conn.commit()
    cur.close()
    conn.close()
    print()
    print(f"Processed: {done} | skipped: {skipped} | "
          f"{'DRY-RUN (no writes)' if args.dry_run else 'written'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
