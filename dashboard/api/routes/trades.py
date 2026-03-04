"""Trade history endpoints."""

from fastapi import APIRouter, HTTPException, Query

from dashboard.api.models import TradeRecord, TradeDetail, AIDecisionRecord
from dashboard.api import queries

router = APIRouter()


@router.get("/trades", response_model=list[TradeRecord])
async def list_trades(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = await queries.get_trades(status=status, limit=limit, offset=offset)
    return [_to_trade_record(r) for r in rows]


@router.get("/trades/{trade_id}", response_model=TradeDetail)
async def get_trade(trade_id: int):
    row = await queries.get_trade_by_id(trade_id)
    if not row:
        raise HTTPException(404, "Trade not found")
    ai_rows = await queries.get_ai_decisions_for_trade(trade_id)
    record = _to_trade_record(row)
    return TradeDetail(
        **record.model_dump(),
        ai_decisions=[_to_ai_record(a) for a in ai_rows],
    )


def _to_trade_record(row: dict) -> TradeRecord:
    return TradeRecord(
        id=row["id"],
        pair=row.get("pair"),
        direction=row.get("direction"),
        setup_type=row.get("setup_type"),
        entry_price=row.get("entry_price"),
        sl_price=row.get("sl_price"),
        tp1_price=row.get("tp1_price"),
        tp2_price=row.get("tp2_price"),
        tp3_price=row.get("tp3_price"),
        actual_entry=row.get("actual_entry"),
        actual_exit=row.get("actual_exit"),
        exit_reason=row.get("exit_reason"),
        position_size=row.get("position_size"),
        pnl_usd=row.get("pnl_usd"),
        pnl_pct=row.get("pnl_pct"),
        ai_confidence=row.get("ai_confidence"),
        opened_at=str(row["opened_at"]) if row.get("opened_at") else None,
        closed_at=str(row["closed_at"]) if row.get("closed_at") else None,
        status=row.get("status"),
    )


def _to_ai_record(row: dict) -> AIDecisionRecord:
    return AIDecisionRecord(
        id=row["id"],
        trade_id=row.get("trade_id"),
        confidence=row.get("confidence"),
        reasoning=row.get("reasoning"),
        adjustments=row.get("adjustments"),
        warnings=row.get("warnings"),
        created_at=str(row["created_at"]) if row.get("created_at") else None,
    )
