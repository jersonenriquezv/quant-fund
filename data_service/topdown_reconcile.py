"""Match Bybit trades to the /topdown edge alerts that produced them.

The live falsification of the `/topdown` edge (docs/audits/topdown-edge-expectancy-2026-05-25.md)
requires N>=30 Bybit trades that were actually *taken* from the BTC/ETH edge alerts in
`signal_scanner_alerts` (auto_setup_type='topdown_edge'). Tagging those trades by hand never
happened (0/63), so the watcher links them automatically on open via the STRICT rule below,
and scripts/reconcile_topdown_falsification.py reuses the same rule to backfill closed trades.

STRICT match (user choice 2026-06-25):
    - same pair (ETHUSDT -> ETH/USDT) and direction (Buy->long, Sell->short)
    - alert.auto_setup_type = 'topdown_edge'
    - alert scanned within MATCH_WINDOW_HOURS *before* the trade opened
      (a limit order placed off an alert normally fills within this window)
    - trade entry within MATCH_ENTRY_TOL_PCT of the alert's planned entry
    - if several alerts qualify, the most recent one wins (closest to the fill)

This module is dependency-light (one psycopg2 connection in) so both the watcher daemon and
offline scripts can call it. It NEVER raises on a miss — returns None — so a match attempt can
never block a trade open.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Strict thresholds. Keep in sync with the audit + SYSTEM_BASELINE if retuned.
MATCH_WINDOW_HOURS = 36.0   # alert must have fired within this many hours before open
MATCH_ENTRY_TOL_PCT = 0.6   # |trade_entry - alert_entry| / alert_entry, in percent


def bybit_symbol_to_pair(symbol: str) -> str | None:
    """ETHUSDT -> ETH/USDT. Returns None for non-USDT or unknown symbols."""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return None


def side_to_direction(side: str) -> str | None:
    """Bybit Buy/Sell -> long/short."""
    s = side.lower()
    if s == "buy":
        return "long"
    if s == "sell":
        return "short"
    return None


@dataclass(frozen=True)
class AlertMatch:
    alert_id: int
    pair: str
    direction: str
    entry: float | None
    sl: float | None
    tp: float | None
    rr: float | None
    scanned_at: datetime
    entry_diff_pct: float | None  # |trade-alert|/alert * 100, None if alert entry missing
    lead_hours: float             # hours between alert scan and trade open


def find_matching_alert(
    cur,
    *,
    symbol: str,
    side: str,
    entry_price: float,
    opened_at: datetime,
) -> AlertMatch | None:
    """Find the topdown_edge alert this trade was most likely taken from.

    `cur` is an open psycopg2 cursor (any row factory). Returns the best AlertMatch or None.
    Pure read; never mutates.
    """
    pair = bybit_symbol_to_pair(symbol)
    direction = side_to_direction(side)
    if pair is None or direction is None or not entry_price:
        return None
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)

    cur.execute(
        """
        SELECT id, pair, direction, entry, sl, tp, rr, scanned_at
        FROM signal_scanner_alerts
        WHERE auto_setup_type = 'topdown_edge'
          AND pair = %s
          AND direction = %s
          AND scanned_at <= %s
          AND scanned_at >= %s - (%s * INTERVAL '1 hour')
        ORDER BY scanned_at DESC
        """,
        (pair, direction, opened_at, opened_at, MATCH_WINDOW_HOURS),
    )
    rows = cur.fetchall()
    for row in rows:
        # Tolerate both tuple and dict row factories.
        if isinstance(row, dict):
            aid, p, d, a_entry, sl, tp, rr, scanned = (
                row["id"], row["pair"], row["direction"], row["entry"],
                row["sl"], row["tp"], row["rr"], row["scanned_at"],
            )
        else:
            aid, p, d, a_entry, sl, tp, rr, scanned = row

        entry_diff_pct = None
        if a_entry:
            entry_diff_pct = abs(entry_price - a_entry) / a_entry * 100.0
            if entry_diff_pct > MATCH_ENTRY_TOL_PCT:
                continue  # too far from the planned entry — not this alert
        if scanned.tzinfo is None:
            scanned = scanned.replace(tzinfo=timezone.utc)
        lead_hours = (opened_at - scanned).total_seconds() / 3600.0
        return AlertMatch(
            alert_id=aid, pair=p, direction=d, entry=a_entry, sl=sl, tp=tp, rr=rr,
            scanned_at=scanned, entry_diff_pct=entry_diff_pct, lead_hours=lead_hours,
        )
    return None
