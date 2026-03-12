"""Risk state endpoints."""

import json

from fastapi import APIRouter, Query

from dashboard.api import database as db
from dashboard.api.models import RiskState, RiskEventRecord
from dashboard.api import queries

router = APIRouter()


@router.get("/risk", response_model=RiskState)
async def get_risk_state(limit: int = Query(20, ge=1, le=100)):
    daily_dd = None
    weekly_dd = None
    cooldown_until = None
    open_positions = 0

    if db.redis_client:
        dd_raw = await db.redis_client.get("qf:bot:daily_dd")
        if dd_raw:
            daily_dd = float(dd_raw)

        wd_raw = await db.redis_client.get("qf:bot:weekly_dd")
        if wd_raw:
            weekly_dd = float(wd_raw)

        cd_raw = await db.redis_client.get("qf:bot:cooldown_until")
        if cd_raw:
            cooldown_until = int(float(cd_raw))

        pos_raw = await db.redis_client.get("qf:bot:positions")
        if pos_raw:
            positions = json.loads(pos_raw)
            # Count only filled positions, not pending unfilled limit orders
            open_positions = sum(
                1 for p in positions if p.get("phase") != "pending_entry"
            )

    events = await queries.get_recent_risk_events(limit=limit)

    return RiskState(
        daily_dd_pct=daily_dd,
        weekly_dd_pct=weekly_dd,
        open_positions=open_positions,
        max_positions=3,
        cooldown_until=cooldown_until,
        recent_events=[
            RiskEventRecord(
                id=e["id"],
                event_type=e.get("event_type"),
                details=e.get("details"),
                created_at=str(e["created_at"]) if e.get("created_at") else None,
            )
            for e in events
        ],
    )
