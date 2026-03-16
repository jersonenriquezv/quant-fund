"""Liquidation heatmap endpoint — estimated liquidation levels from OI + candles."""

import json

from fastapi import APIRouter, HTTPException

from dashboard.api import database as db
from dashboard.api.models import LiqHeatmapResponse, LiqHeatmapBin
from dashboard.api.queries import get_candles

router = APIRouter()

# --- Estimator constants (self-contained, no external imports) ---

# Leverage tiers and their estimated share of total OI.
# Source: industry research — retail-heavy exchanges skew toward 5-25x.
LEVERAGE_TIERS: list[int] = [5, 10, 25, 50, 100]
LEVERAGE_WEIGHTS: list[float] = [0.30, 0.30, 0.20, 0.15, 0.05]
MAINTENANCE_MARGIN: float = 0.004

# Price bin widths
BIN_SIZE_BTC: float = 50.0
BIN_SIZE_ETH: float = 2.0

# How many 5m candles to use (~17 hours at 200)
CANDLE_COUNT: int = 200

# Redis cache TTL
CACHE_TTL: int = 30

CACHE_KEY_PREFIX = "qf:liq_heatmap"


def _estimate_liquidation_levels(
    candles: list[dict], oi_usd: float, pair: str
) -> list[dict]:
    """Estimate liquidation level distribution from candles and OI.

    Returns list of {price, liq_long_usd, liq_short_usd} sorted by price.
    """
    if not candles or oi_usd <= 0:
        return []

    if "BTC" in pair:
        bin_size = BIN_SIZE_BTC
    elif "SOL" in pair:
        bin_size = 0.5
    elif "DOGE" in pair:
        bin_size = 0.002
    else:
        bin_size = BIN_SIZE_ETH

    # Weight OI distribution by candle volume
    total_volume = sum(c.get("volume_quote", c.get("volume", 0)) for c in candles)
    if total_volume <= 0:
        total_volume = len(candles)
        volume_weights = [1.0 / total_volume] * len(candles)
    else:
        volume_weights = [
            c.get("volume_quote", c.get("volume", 0)) / total_volume for c in candles
        ]

    # Accumulate liquidation USD into bins
    bins: dict[float, list[float]] = {}

    for candle, vol_weight in zip(candles, volume_weights):
        close = candle.get("close", 0)
        if close <= 0:
            continue

        for leverage, lev_weight in zip(LEVERAGE_TIERS, LEVERAGE_WEIGHTS):
            move_pct = (1.0 / leverage) * (1.0 - MAINTENANCE_MARGIN)
            liq_long_price = close * (1.0 - move_pct)
            liq_short_price = close * (1.0 + move_pct)

            usd_alloc = oi_usd * vol_weight * lev_weight

            long_bin = round(liq_long_price / bin_size) * bin_size
            if long_bin not in bins:
                bins[long_bin] = [0.0, 0.0]
            bins[long_bin][0] += usd_alloc

            short_bin = round(liq_short_price / bin_size) * bin_size
            if short_bin not in bins:
                bins[short_bin] = [0.0, 0.0]
            bins[short_bin][1] += usd_alloc

    return [
        {"price": price, "liq_long_usd": vals[0], "liq_short_usd": vals[1]}
        for price, vals in sorted(bins.items())
    ]


@router.get("/liquidation/heatmap/{pair:path}", response_model=LiqHeatmapResponse)
async def get_liquidation_heatmap(pair: str):
    if not db.redis_client:
        raise HTTPException(503, "Redis unavailable")

    # Check cache first
    cache_key = f"{CACHE_KEY_PREFIX}:{pair}"
    cached = await db.redis_client.get(cache_key)
    if cached:
        return LiqHeatmapResponse(**json.loads(cached))

    # Get current price from latest candle
    candle_key = f"qf:candle:{pair}:5m"
    candle_raw = await db.redis_client.get(candle_key)
    if not candle_raw:
        raise HTTPException(404, f"No candle data for {pair}")
    current_price = json.loads(candle_raw).get("close", 0)
    if current_price <= 0:
        raise HTTPException(404, f"Invalid price for {pair}")

    # Get OI from Redis
    oi_key = f"qf:oi:{pair}"
    oi_raw = await db.redis_client.get(oi_key)
    if not oi_raw:
        return LiqHeatmapResponse(pair=pair, current_price=current_price, bins=[])
    oi_usd = json.loads(oi_raw).get("oi_usd", 0)
    if oi_usd <= 0:
        return LiqHeatmapResponse(pair=pair, current_price=current_price, bins=[])

    # Get candles from PostgreSQL
    if not db.pg_pool:
        raise HTTPException(503, "PostgreSQL unavailable")

    raw_candles = await get_candles(pair, "5m", CANDLE_COUNT)
    if not raw_candles:
        raise HTTPException(404, f"No candle history for {pair}")

    # Compute liquidation levels
    liq_bins = _estimate_liquidation_levels(raw_candles, oi_usd, pair)

    response = LiqHeatmapResponse(
        pair=pair,
        current_price=current_price,
        bins=[
            LiqHeatmapBin(
                price=b["price"],
                liq_long_usd=b["liq_long_usd"],
                liq_short_usd=b["liq_short_usd"],
            )
            for b in liq_bins
        ],
    )

    # Cache result
    await db.redis_client.set(
        cache_key,
        json.dumps(response.model_dump()),
        ex=CACHE_TTL,
    )

    return response
