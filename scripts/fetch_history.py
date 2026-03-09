#!/usr/bin/env python3
"""
Fetch historical candles from OKX and store in PostgreSQL.

Downloads up to 90 days of candle data for all pairs/timeframes.
Uses ExchangeClient.backfill_candles() with OKX REST pagination.
PostgresStore.store_candles() handles dedup via ON CONFLICT.

Usage:
    python scripts/fetch_history.py --days 90
    python scripts/fetch_history.py --days 60 --pair BTC/USDT
    python scripts/fetch_history.py --days 30 --timeframe 5m
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import settings
from data_service.exchange_client import ExchangeClient
from data_service.data_store import PostgresStore
from shared.logger import setup_logger

logger = setup_logger("fetch_history")

# Candles per day by timeframe
_CANDLES_PER_DAY = {
    "5m": 288,
    "15m": 96,
    "1h": 24,
    "4h": 6,
}


def fetch_history(days: int, pairs: list[str] | None = None,
                  timeframes: list[str] | None = None) -> None:
    if pairs is None:
        pairs = settings.TRADING_PAIRS
    if timeframes is None:
        timeframes = settings.HTF_TIMEFRAMES + settings.LTF_TIMEFRAMES

    # Connect to PostgreSQL
    pg = PostgresStore()
    if not pg.connect():
        logger.error("Cannot connect to PostgreSQL — aborting")
        sys.exit(1)

    # Initialize exchange client (uses production endpoint for market data)
    client = ExchangeClient()

    total_stored = 0
    total_fetched = 0

    for pair in pairs:
        for tf in timeframes:
            candles_needed = _CANDLES_PER_DAY.get(tf, 288) * days
            logger.info(f"Fetching {pair} {tf}: ~{candles_needed} candles ({days} days)")

            candles = client.backfill_candles(pair, tf, count=candles_needed)
            total_fetched += len(candles)

            if candles:
                stored = pg.store_candles(candles)
                total_stored += stored
                logger.info(f"  {pair} {tf}: fetched={len(candles)}, "
                            f"new={stored}")
            else:
                logger.warning(f"  {pair} {tf}: no candles returned")

    pg.close()

    print()
    print(f"Done. Fetched {total_fetched} candles, stored {total_stored} new.")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch historical candles from OKX into PostgreSQL")
    parser.add_argument("--days", type=int, default=90,
                        help="Days of history to fetch (default: 90)")
    parser.add_argument("--pair", type=str, default=None,
                        help="Single pair (e.g. BTC/USDT)")
    parser.add_argument("--timeframe", type=str, default=None,
                        help="Single timeframe (e.g. 5m)")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else None
    timeframes = [args.timeframe] if args.timeframe else None
    fetch_history(days=args.days, pairs=pairs, timeframes=timeframes)


if __name__ == "__main__":
    main()
