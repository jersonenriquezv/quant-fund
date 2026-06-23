"""Shadow-mode endpoints — read-only view over ml_setups.

The live `/trades` table froze on 2026-04-09 (bot went shadow-only 2026-04-15).
All current activity (engine1, legacy A/B/D/F, benchmarks) lives in ml_setups
as theoretical shadow trades. These endpoints mirror the real-trades views.

Read-only: never mutate ml_setups (bot owns it). pnl_usd is already net of fees.
"""

from fastapi import APIRouter, Query

from dashboard.api.models import ShadowTradeRecord, ShadowStats, ShadowSetupBreakdown
from dashboard.api import queries

router = APIRouter()


@router.get("/shadow/trades", response_model=list[ShadowTradeRecord])
async def list_shadow_trades(
    status: str | None = Query(None, pattern="^(open|closed)$"),
    setup_type: str | None = None,
    experiment_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = await queries.get_shadow_trades(
        status=status, setup_type=setup_type, experiment_id=experiment_id,
        limit=limit, offset=offset,
    )
    return [_to_shadow_record(r) for r in rows]


@router.get("/shadow/stats", response_model=ShadowStats)
async def shadow_stats(
    setup_type: str | None = None,
    experiment_id: str | None = None,
):
    d = await queries.get_shadow_stats(setup_type=setup_type, experiment_id=experiment_id)
    return ShadowStats(
        experiment_id=d.get("experiment_id"),
        total_trades=d["total_trades"],
        winning_trades=d["winning_trades"],
        losing_trades=d["losing_trades"],
        win_rate=d["win_rate"],
        profit_factor=d["profit_factor"],
        total_pnl_usd=d["total_pnl_usd"],
        avg_pnl_pct=d["avg_pnl_pct"],
        best_trade_pct=d["best_trade_pct"],
        worst_trade_pct=d["worst_trade_pct"],
        by_setup_type=[ShadowSetupBreakdown(**b) for b in d["by_setup_type"]],
    )


def _to_shadow_record(row: dict) -> ShadowTradeRecord:
    status = "closed" if row.get("outcome_type") else "open"
    return ShadowTradeRecord(
        setup_id=str(row["setup_id"]) if row.get("setup_id") is not None else None,
        setup_type=row.get("setup_type"),
        pair=row.get("pair"),
        direction=row.get("direction"),
        entry_price=row.get("entry_price"),
        sl_price=row.get("sl_price"),
        tp1_price=row.get("tp1_price"),
        tp2_price=row.get("tp2_price"),
        actual_entry=row.get("actual_entry"),
        entry_distance_pct=row.get("entry_distance_pct"),
        sl_distance_pct=row.get("sl_distance_pct"),
        outcome_type=row.get("outcome_type"),
        pnl_pct=row.get("pnl_pct"),
        pnl_usd=row.get("pnl_usd"),
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        resolved_at=str(row["resolved_at"]) if row.get("resolved_at") else None,
        status=status,
    )
