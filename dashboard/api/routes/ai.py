"""AI decision log endpoints."""

from fastapi import APIRouter, Query

from dashboard.api.models import AIDecisionRecord
from dashboard.api import queries

router = APIRouter()


@router.get("/ai/decisions", response_model=list[AIDecisionRecord])
async def list_ai_decisions(limit: int = Query(20, ge=1, le=100)):
    rows = await queries.get_recent_ai_decisions(limit=limit)
    return [
        AIDecisionRecord(
            id=r["id"],
            trade_id=r.get("trade_id"),
            pair=r.get("pair"),
            direction=r.get("direction"),
            setup_type=r.get("setup_type"),
            approved=r.get("approved"),
            confidence=r.get("confidence"),
            reasoning=r.get("reasoning"),
            adjustments=r.get("adjustments"),
            warnings=r.get("warnings"),
            created_at=str(r["created_at"]) if r.get("created_at") else None,
        )
        for r in rows
    ]
