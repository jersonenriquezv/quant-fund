"""Sync Bybit manual trades into Postgres.

Usage:
    python scripts/sync_bybit.py                    # linear, last 7 days
    python scripts/sync_bybit.py --days 30          # last 30 days
    python scripts/sync_bybit.py --days 30 --categories linear spot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.bybit_sync import BybitSync


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="lookback window in days (max 7 per Bybit API call, but pagination handles longer)")
    parser.add_argument("--categories", nargs="+", default=["linear"], choices=["linear", "spot", "inverse"])
    args = parser.parse_args()

    sync = BybitSync()
    counts = sync.sync_all(days=args.days, categories=tuple(args.categories))

    print(f"\n=== Bybit Sync Complete (last {args.days}d) ===")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
