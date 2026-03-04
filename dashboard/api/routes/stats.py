"""Trading stats endpoints."""

from fastapi import APIRouter

from dashboard.api.models import StatsResponse
from dashboard.api import queries

router = APIRouter()


@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    data = await queries.get_trade_stats()
    return StatsResponse(**data)
