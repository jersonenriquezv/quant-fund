"""Strategy state endpoints — order blocks and HTF bias from Redis."""

import json

from fastapi import APIRouter

from dashboard.api import database as db
from dashboard.api.models import OrderBlockRecord, HTFBiasResponse

router = APIRouter()


@router.get("/strategy/order-blocks", response_model=list[OrderBlockRecord])
async def get_order_blocks():
    if not db.redis_client:
        return []

    raw = await db.redis_client.get("qf:bot:order_blocks")
    if not raw:
        return []

    return json.loads(raw)


@router.get("/strategy/htf-bias", response_model=HTFBiasResponse)
async def get_htf_bias():
    if not db.redis_client:
        return HTFBiasResponse(bias={})

    raw = await db.redis_client.get("qf:bot:htf_bias")
    if not raw:
        return HTFBiasResponse(bias={})

    return HTFBiasResponse(bias=json.loads(raw))
