"""Whale movement endpoints — reads cached data from Redis."""

import json
import time

from fastapi import APIRouter, Query

from dashboard.api import database as db
from dashboard.api.models import WhaleMovementRecord

router = APIRouter()


@router.get("/whales", response_model=list[WhaleMovementRecord])
async def get_whales(hours: int = Query(24, ge=1, le=168)):
    if not db.redis_client:
        return []

    raw = await db.redis_client.get("qf:bot:whale_movements")
    if not raw:
        return []

    records = json.loads(raw)
    cutoff = int((time.time() - hours * 3600) * 1000)
    return [r for r in records if r.get("timestamp", 0) >= cutoff]
