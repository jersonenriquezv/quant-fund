"""Market data endpoints — live price, funding, OI from Redis."""

import json

from fastapi import APIRouter, HTTPException

from dashboard.api import database as db
from dashboard.api.models import MarketData

router = APIRouter()


@router.get("/market/{pair:path}", response_model=MarketData)
async def get_market(pair: str):
    if not db.redis_client:
        raise HTTPException(503, "Redis unavailable")

    # Get latest candle for price
    candle_key = f"qf:candle:{pair}:5m"
    candle_raw = await db.redis_client.get(candle_key)

    price = None
    change_pct = None
    if candle_raw:
        candle = json.loads(candle_raw)
        price = candle.get("close")
        if candle.get("open") and candle["open"] > 0:
            change_pct = (candle["close"] - candle["open"]) / candle["open"] * 100

    # Get funding rate
    fr_key = f"qf:funding:{pair}"
    fr_raw = await db.redis_client.get(fr_key)
    funding_rate = None
    next_funding_rate = None
    next_funding_time = None
    if fr_raw:
        fr = json.loads(fr_raw)
        funding_rate = fr.get("rate")
        next_funding_rate = fr.get("next_rate")
        next_funding_time = fr.get("next_funding_time")

    # Get OI
    oi_key = f"qf:oi:{pair}"
    oi_raw = await db.redis_client.get(oi_key)
    oi_usd = None
    oi_base = None
    if oi_raw:
        oi = json.loads(oi_raw)
        oi_usd = oi.get("oi_usd")
        oi_base = oi.get("oi_base")

    return MarketData(
        pair=pair,
        price=price,
        change_pct=change_pct,
        funding_rate=funding_rate,
        next_funding_rate=next_funding_rate,
        next_funding_time=next_funding_time,
        oi_usd=oi_usd,
        oi_base=oi_base,
    )
