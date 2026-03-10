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

    # Fetch historical funding rates
    logger.info("Fetching historical funding rates...")
    import time
    total_funding = 0
    for pair in pairs:
        since_ms = int((time.time() - days * 86400) * 1000)
        all_rates = []
        current_since = since_ms
        while True:
            batch = client.fetch_funding_rate_history(pair, since_ms=current_since, limit=100)
            if not batch:
                break
            all_rates.extend(batch)
            # Paginate forward
            last_ts = batch[-1]["timestamp"]
            if last_ts <= current_since:
                break
            current_since = last_ts + 1
            if len(batch) < 100:
                break
            time.sleep(0.2)  # Rate limit courtesy

        if all_rates:
            records = [(pair, r["timestamp"], r["rate"], r["next_rate"]) for r in all_rates]
            inserted = pg.store_funding_rates_batch(records)
            total_funding += inserted
            logger.info(f"  {pair}: fetched={len(all_rates)} funding rates, new={inserted}")

    # Fetch historical OI (1h resolution, OKX limits to ~30 days back)
    oi_days = min(days, 30)
    logger.info(f"Fetching historical open interest ({oi_days}d at 1h resolution)...")
    total_oi = 0
    for pair in pairs:
        since_ms = int((time.time() - oi_days * 86400) * 1000)
        all_oi = []
        current_since = since_ms
        while True:
            batch = client.fetch_open_interest_history(
                pair, since_ms=current_since, limit=100, timeframe="1h"
            )
            if not batch:
                break
            all_oi.extend(batch)
            last_ts = batch[-1]["timestamp"]
            if last_ts <= current_since:
                break
            current_since = last_ts + 1
            if len(batch) < 100:
                break
            time.sleep(0.2)

        if all_oi:
            records = [(pair, r["timestamp"], r["oi_contracts"], r["oi_base"], r["oi_usd"])
                       for r in all_oi]
            inserted = pg.store_open_interest_batch(records)
            total_oi += inserted
            logger.info(f"  {pair}: fetched={len(all_oi)} OI snapshots, new={inserted}")

    pg.close()

    print()
    print(f"Done. Candles: fetched={total_fetched}, new={total_stored}. "
          f"Funding: {total_funding} new. OI: {total_oi} new.")


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
