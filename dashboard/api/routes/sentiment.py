"""Sentiment endpoint — Fear & Greed Index from Redis cache."""

import json

from fastapi import APIRouter

from dashboard.api import database as db
from dashboard.api.models import SentimentResponse

router = APIRouter()


@router.get("/sentiment", response_model=SentimentResponse)
async def get_sentiment():
    if not db.redis_client:
        return SentimentResponse()

    raw = await db.redis_client.get("qf:bot:news:fear_greed")
    if not raw:
        return SentimentResponse()

    try:
        data = json.loads(raw)
        return SentimentResponse(score=data.get("score"), label=data.get("label"))
    except (json.JSONDecodeError, KeyError):
        return SentimentResponse()
