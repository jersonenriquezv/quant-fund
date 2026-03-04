"""Health check endpoint."""

from fastapi import APIRouter

from dashboard.api import database as db
from dashboard.api.models import HealthResponse
from dashboard.api import queries

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health():
    pg_ok = await queries.pg_ping()

    redis_ok = False
    try:
        if db.redis_client:
            await db.redis_client.ping()
            redis_ok = True
    except Exception:
        pass

    status = "ok" if (pg_ok and redis_ok) else "degraded"
    return HealthResponse(status=status, postgres=pg_ok, redis=redis_ok)
