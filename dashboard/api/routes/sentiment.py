"""Sentiment endpoints — Fear & Greed Index + news headlines from Redis cache."""

import json

from fastapi import APIRouter

from dashboard.api import database as db
from dashboard.api.models import (
    HeadlineRecord,
    HeadlinesResponse,
    SentimentResponse,
)

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


@router.get("/headlines", response_model=HeadlinesResponse)
async def get_headlines():
    if not db.redis_client:
        return HeadlinesResponse()

    headlines: list[HeadlineRecord] = []
    for asset in ("BTC", "ETH"):
        raw = await db.redis_client.get(f"qf:bot:news:headlines:{asset}")
        if not raw:
            continue
        try:
            items = json.loads(raw)
            for h in items:
                headlines.append(HeadlineRecord(
                    title=h["title"],
                    source=h["source"],
                    timestamp=h["timestamp"],
                    category=h["category"],
                    url=h.get("url", ""),
                    sentiment=h.get("sentiment"),
                ))
        except (json.JSONDecodeError, KeyError):
            continue

    # Sort by timestamp descending (newest first)
    headlines.sort(key=lambda h: h.timestamp, reverse=True)
    return HeadlinesResponse(headlines=headlines[:10])
