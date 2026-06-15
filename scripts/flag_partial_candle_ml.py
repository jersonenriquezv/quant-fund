"""Flag ml_setups whose trigger bar was a partial (forming) candle.

Part of the partial-candle backfill fix (docs/plans/partial-candle-backfill-fix.md,
Phase 3). Precise per-row identification of ALL contaminated rows is not
recoverable: the partial-bar list was overwritten by the Phase 2 repair and the
WS-reconnect log (`bot_metrics.ws_reconnect`) only goes back to 2026-05-18 and
does not capture startup backfills. This script tags the recoverable HIGH-RISK
subset — rows whose detection (trigger) bar coincided with the bar that was
*forming* at a known reconnect, i.e. the bar most likely stored partial.

Sets ``ml_setups.data_quality = 'partial_candle_risk'`` (migration 22) on those
rows. Idempotent. The broader, unquantifiable contamination is documented as a
caveat in SYSTEM_BASELINE §7 (this is a lower bound, not the full set).

Run: source venv/bin/activate && PYTHONPATH=. python scripts/flag_partial_candle_ml.py [--apply]
Without --apply it is a dry run (reports counts, writes nothing).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from data_service.data_store import PostgresStore

TAG = "partial_candle_risk"
# Bot candle timeframes whose forming bar a reconnect could have frozen.
TF_MS = {"4h": 14_400_000, "1h": 3_600_000, "15m": 900_000, "5m": 300_000}


def forming_bar_timestamps(cur) -> set[int]:
    """For each WS reconnect, the open-ts of the bar forming at that instant,
    across all candle timeframes. These are the bars most likely stored partial."""
    cur.execute(
        "SELECT EXTRACT(EPOCH FROM created_at) * 1000 FROM bot_metrics "
        "WHERE metric_name = 'ws_reconnect'")
    recon = [int(r[0]) for r in cur.fetchall()]
    forming: set[int] = set()
    for r in recon:
        for ms in TF_MS.values():
            forming.add((r // ms) * ms)  # floor to bar open
    return forming, len(recon)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the tag (default: dry run)")
    args = ap.parse_args()

    store = PostgresStore()
    if not store.connect():  # connect() runs migrations incl. data_quality col
        print("FATAL: cannot connect to PostgreSQL")
        sys.exit(2)

    with store._conn.cursor() as cur:
        forming, n_recon = forming_bar_timestamps(cur)
        cur.execute("SELECT id, setup_type, timestamp FROM ml_setups "
                    "WHERE timestamp IS NOT NULL")
        rows = cur.fetchall()
        hits = [r for r in rows if int(r[2]) in forming]

        print(f"reconnect events: {n_recon}  forming bars: {len(forming)}")
        print(f"ml_setups scanned: {len(rows)}  high-risk (trigger==forming): {len(hits)}")
        print(f"  by setup_type: {dict(Counter(r[1] for r in hits))}")

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to write "
                  f"data_quality='{TAG}' on {len(hits)} rows.")
            return

        ids = [r[0] for r in hits]
        cur.execute(
            "UPDATE ml_setups SET data_quality = %s "
            "WHERE id = ANY(%s) AND (data_quality IS NULL OR data_quality <> %s)",
            (TAG, ids, TAG))
        print(f"\nAPPLIED — tagged {cur.rowcount} rows data_quality='{TAG}' "
              f"(idempotent; already-tagged rows skipped).")


if __name__ == "__main__":
    main()
