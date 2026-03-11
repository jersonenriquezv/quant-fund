"""
Shared test fixtures and helpers for strategy service tests.

Provides:
- make_candle(): Create a single Candle with sensible defaults
- make_candle_series(): Create a series of candles with controllable price action
"""

import time
from typing import Optional

import pytest

from shared.models import (
    Candle, MarketSnapshot, FundingRate, OpenInterest,
    CVDSnapshot, OIFlushEvent,
)


def make_candle(
    open: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: float = 10.0,
    volume_quote: float = 1000.0,
    pair: str = "BTC/USDT",
    timeframe: str = "15m",
    timestamp: Optional[int] = None,
    confirmed: bool = True,
) -> Candle:
    """Create a single Candle with sensible defaults."""
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    return Candle(
        timestamp=timestamp,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        volume_quote=volume_quote,
        pair=pair,
        timeframe=timeframe,
        confirmed=confirmed,
    )


def make_candle_series(
    base_price: float = 100.0,
    count: int = 50,
    pair: str = "BTC/USDT",
    timeframe: str = "15m",
    start_ts: int = 1_000_000_000_000,
    interval_ms: int = 900_000,  # 15 min
    price_changes: Optional[list[float]] = None,
    volume: float = 10.0,
) -> list[Candle]:
    """Create a series of candles with controllable price action.

    Args:
        base_price: Starting price.
        count: Number of candles to generate.
        pair: Trading pair.
        timeframe: Candle timeframe.
        start_ts: Starting timestamp in ms.
        interval_ms: Time between candles in ms.
        price_changes: Optional list of price deltas. If shorter than count,
            remaining candles use +0.1 change. If None, all use +0.1.
        volume: Base volume per candle.

    Returns:
        List of Candle objects, oldest first.
    """
    candles = []
    current_price = base_price

    for i in range(count):
        if price_changes and i < len(price_changes):
            delta = price_changes[i]
        else:
            delta = 0.1  # Default slight uptrend

        open_price = current_price
        close_price = current_price + delta
        high_price = max(open_price, close_price) + abs(delta) * 0.5
        low_price = min(open_price, close_price) - abs(delta) * 0.5

        candles.append(Candle(
            timestamp=start_ts + (i * interval_ms),
            open=round(open_price, 2),
            high=round(high_price, 2),
            low=round(low_price, 2),
            close=round(close_price, 2),
            volume=volume,
            volume_quote=volume * current_price,
            pair=pair,
            timeframe=timeframe,
            confirmed=True,
        ))

        current_price = close_price

    return candles


def make_market_snapshot(
    pair: str = "BTC/USDT",
    funding_rate: float = 0.0001,
    oi_usd: float = 1_000_000.0,
    cvd_15m: float = 100.0,
    oi_flushes: Optional[list[OIFlushEvent]] = None,
) -> MarketSnapshot:
    """Create a MarketSnapshot with optional market data."""
    ts = int(time.time() * 1000)

    funding = FundingRate(
        timestamp=ts,
        pair=pair,
        rate=funding_rate,
        next_rate=funding_rate,
        next_funding_time=ts + 28800000,
    )

    oi = OpenInterest(
        timestamp=ts,
        pair=pair,
        oi_contracts=1000.0,
        oi_base=10.0,
        oi_usd=oi_usd,
    )

    cvd = CVDSnapshot(
        timestamp=ts,
        pair=pair,
        cvd_5m=cvd_15m / 3,
        cvd_15m=cvd_15m,
        cvd_1h=cvd_15m * 4,
        buy_volume=500.0,
        sell_volume=400.0,
    )

    return MarketSnapshot(
        pair=pair,
        timestamp=ts,
        funding=funding,
        oi=oi,
        cvd=cvd,
        recent_oi_flushes=oi_flushes or [],
        whale_movements=[],
    )
