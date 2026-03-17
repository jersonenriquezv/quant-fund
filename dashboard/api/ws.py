"""WebSocket endpoint for live updates — polls Redis every 2 seconds."""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from dashboard.api import database as db

router = APIRouter()

PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT", "XRP/USDT", "LINK/USDT", "AVAX/USDT"]


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            data = await _gather_live_data()
            await ws.send_json(data)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass


async def _gather_live_data() -> dict:
    """Pull latest prices + positions from Redis."""
    result: dict = {"prices": {}, "positions": []}

    if not db.redis_client:
        return result

    for pair in PAIRS:
        candle_raw = await db.redis_client.get(f"qf:candle:{pair}:5m")
        if candle_raw:
            candle = json.loads(candle_raw)
            result["prices"][pair] = {
                "price": candle.get("close"),
                "open": candle.get("open"),
                "high": candle.get("high"),
                "low": candle.get("low"),
                "timestamp": candle.get("timestamp"),
            }

    pos_raw = await db.redis_client.get("qf:bot:positions")
    if pos_raw:
        result["positions"] = json.loads(pos_raw)

    return result
