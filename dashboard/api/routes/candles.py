"""Candle data endpoints for sparklines."""

from fastapi import APIRouter, Query

from dashboard.api.models import CandleRecord
from dashboard.api import queries

router = APIRouter()


@router.get("/candles/{pair:path}/{timeframe}", response_model=list[CandleRecord])
async def get_candles(pair: str, timeframe: str, count: int = Query(100, ge=1, le=500)):
    rows = await queries.get_candles(pair, timeframe, count)
    return [
        CandleRecord(
            timestamp=r["timestamp"],
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
        )
        for r in rows
    ]
