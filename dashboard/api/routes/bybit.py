"""Bybit manual trade log endpoints.

Read/write annotations, list trades, summary stats, weekly review storage.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from dashboard.api import database as db

router = APIRouter(prefix="/bybit", tags=["bybit"])


class AnnotationUpdate(BaseModel):
    setup_type: str | None = None
    confluences: list[str] | None = None
    confidence: int | None = Field(default=None, ge=1, le=5)
    thesis_pre: str | None = None
    lesson_post: str | None = None
    emotional_state: str | None = None
    grade_self: str | None = Field(default=None, pattern=r"^[ABCDF]$")
    screenshot_url: str | None = None


class AnnotationOut(BaseModel):
    id: int
    symbol: str
    side: str
    opened_at: datetime
    entry_price: float | None
    size: float | None
    leverage: float | None
    notional_value: float | None
    setup_type: str | None
    confluences: list[str] | None
    confidence: int | None
    thesis_pre: str | None
    lesson_post: str | None
    emotional_state: str | None
    grade_self: str | None
    screenshot_url: str | None
    context_snapshot: dict[str, Any] | None
    closed_at: datetime | None
    exit_price: float | None
    pnl_usd: float | None
    pnl_pct: float | None
    status: str
    annotated_at: datetime | None


def _row_to_out(r: dict) -> AnnotationOut:
    confluences = r.get("confluences")
    if isinstance(confluences, str):
        try:
            confluences = json.loads(confluences)
        except Exception:
            confluences = None
    context = r.get("context_snapshot")
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except Exception:
            context = None
    return AnnotationOut(
        id=r["id"],
        symbol=r["symbol"],
        side=r["side"],
        opened_at=r["opened_at"],
        entry_price=r.get("entry_price"),
        size=r.get("size"),
        leverage=r.get("leverage"),
        notional_value=r.get("notional_value"),
        setup_type=r.get("setup_type"),
        confluences=confluences,
        confidence=r.get("confidence"),
        thesis_pre=r.get("thesis_pre"),
        lesson_post=r.get("lesson_post"),
        emotional_state=r.get("emotional_state"),
        grade_self=r.get("grade_self"),
        screenshot_url=r.get("screenshot_url"),
        context_snapshot=context,
        closed_at=r.get("closed_at"),
        exit_price=r.get("exit_price"),
        pnl_usd=r.get("pnl_usd"),
        pnl_pct=r.get("pnl_pct"),
        status=r.get("status") or "open",
        annotated_at=r.get("annotated_at"),
    )


@router.get("/annotations", response_model=list[AnnotationOut])
async def list_annotations(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    symbol: str | None = None,
):
    async with db.pg_pool.acquire() as conn:
        params: list[Any] = []
        where = []
        if status:
            params.append(status)
            where.append(f"status = ${len(params)}")
        if symbol:
            params.append(symbol)
            where.append(f"symbol = ${len(params)}")
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, offset])
        rows = await conn.fetch(
            f"""
            SELECT * FROM bybit_trade_annotations
            {where_clause}
            ORDER BY opened_at DESC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}
            """,
            *params,
        )
    return [_row_to_out(dict(r)) for r in rows]


@router.get("/annotations/{annotation_id}", response_model=AnnotationOut)
async def get_annotation(annotation_id: int):
    async with db.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM bybit_trade_annotations WHERE id = $1", annotation_id
        )
    if not row:
        raise HTTPException(404, f"annotation {annotation_id} not found")
    return _row_to_out(dict(row))


@router.patch("/annotations/{annotation_id}", response_model=AnnotationOut)
async def update_annotation(annotation_id: int, payload: AnnotationUpdate):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields to update")
    async with db.pg_pool.acquire() as conn:
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            vals.append(json.dumps(v) if k == "confluences" and v is not None else v)
            sets.append(f"{k} = ${len(vals)}")
        vals.append(annotation_id)
        set_clause = ", ".join(sets)
        row = await conn.fetchrow(
            f"""
            UPDATE bybit_trade_annotations
            SET {set_clause}, annotated_at = NOW(), updated_at = NOW()
            WHERE id = ${len(vals)}
            RETURNING *
            """,
            *vals,
        )
    if not row:
        raise HTTPException(404, f"annotation {annotation_id} not found")
    return _row_to_out(dict(row))


@router.get("/summary")
async def summary(days: int = Query(30, ge=1, le=365)):
    """Aggregate stats for annotation-linked trades over last N days."""
    async with db.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'closed') AS closed,
                COUNT(*) FILTER (WHERE status = 'open') AS open,
                COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins,
                COUNT(*) FILTER (WHERE pnl_usd < 0) AS losses,
                COALESCE(SUM(pnl_usd), 0) AS total_pnl,
                COALESCE(AVG(pnl_usd) FILTER (WHERE pnl_usd IS NOT NULL), 0) AS avg_pnl,
                COUNT(*) FILTER (WHERE thesis_pre IS NOT NULL AND thesis_pre <> '') AS annotated,
                COUNT(*) FILTER (WHERE grade_self IS NOT NULL) AS graded
            FROM bybit_trade_annotations
            WHERE opened_at >= NOW() - ($1 * INTERVAL '1 day')
            """,
            days,
        )
        by_setup = await conn.fetch(
            """
            SELECT setup_type,
                   COUNT(*) AS n,
                   COALESCE(SUM(pnl_usd), 0) AS pnl,
                   COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins
            FROM bybit_trade_annotations
            WHERE opened_at >= NOW() - ($1 * INTERVAL '1 day')
              AND setup_type IS NOT NULL
            GROUP BY setup_type
            ORDER BY n DESC
            """,
            days,
        )
    total = dict(row) if row else {}
    closed = total.get("closed") or 0
    wr = None
    if closed:
        wr = float(total.get("wins", 0)) / closed * 100
    return {
        "days": days,
        "totals": {
            **{k: (float(v) if isinstance(v, (int, float)) else v) for k, v in total.items()},
            "win_rate_pct": wr,
        },
        "by_setup": [dict(r) for r in by_setup],
    }


class PendingOrderOut(BaseModel):
    id: int
    order_id: str
    symbol: str
    side: str
    order_type: str | None
    stop_order_type: str | None
    qty: float | None
    price: float | None
    trigger_price: float | None
    status: str
    placed_at: datetime
    filled_at: datetime | None
    cancelled_at: datetime | None
    setup_type: str | None
    confluences: list[str] | None
    confidence: int | None
    thesis_pre: str | None
    emotional_state: str | None
    screenshot_url: str | None
    context_snapshot: dict[str, Any] | None
    annotation_id: int | None
    placed_to_fill_sec: int | None
    placed_to_cancel_sec: int | None


class PendingPatch(BaseModel):
    setup_type: str | None = None
    confluences: list[str] | None = None
    confidence: int | None = Field(default=None, ge=1, le=5)
    thesis_pre: str | None = None
    emotional_state: str | None = None
    screenshot_url: str | None = None


def _pending_to_out(r: dict) -> PendingOrderOut:
    confluences = r.get("confluences")
    if isinstance(confluences, str):
        try: confluences = json.loads(confluences)
        except Exception: confluences = None
    context = r.get("context_snapshot")
    if isinstance(context, str):
        try: context = json.loads(context)
        except Exception: context = None
    return PendingOrderOut(
        id=r["id"],
        order_id=r["order_id"],
        symbol=r["symbol"],
        side=r["side"],
        order_type=r.get("order_type"),
        stop_order_type=r.get("stop_order_type"),
        qty=r.get("qty"),
        price=r.get("price"),
        trigger_price=r.get("trigger_price"),
        status=r.get("status") or "pending",
        placed_at=r["placed_at"],
        filled_at=r.get("filled_at"),
        cancelled_at=r.get("cancelled_at"),
        setup_type=r.get("setup_type"),
        confluences=confluences,
        confidence=r.get("confidence"),
        thesis_pre=r.get("thesis_pre"),
        emotional_state=r.get("emotional_state"),
        screenshot_url=r.get("screenshot_url"),
        context_snapshot=context,
        annotation_id=r.get("annotation_id"),
        placed_to_fill_sec=r.get("placed_to_fill_sec"),
        placed_to_cancel_sec=r.get("placed_to_cancel_sec"),
    )


@router.get("/pending", response_model=list[PendingOrderOut])
async def list_pending(status: str | None = "pending", limit: int = Query(50, ge=1, le=200)):
    async with db.pg_pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM bybit_pending_orders WHERE status = $1 ORDER BY placed_at DESC LIMIT $2",
                status, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM bybit_pending_orders ORDER BY placed_at DESC LIMIT $1",
                limit,
            )
    return [_pending_to_out(dict(r)) for r in rows]


@router.get("/pending/{pending_id}", response_model=PendingOrderOut)
async def get_pending(pending_id: int):
    async with db.pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM bybit_pending_orders WHERE id = $1", pending_id)
    if not row:
        raise HTTPException(404, "not found")
    return _pending_to_out(dict(row))


@router.patch("/pending/{pending_id}", response_model=PendingOrderOut)
async def update_pending(pending_id: int, payload: PendingPatch):
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "no fields")
    async with db.pg_pool.acquire() as conn:
        sets: list[str] = []
        vals: list[Any] = []
        for k, v in fields.items():
            vals.append(json.dumps(v) if k == "confluences" and v is not None else v)
            sets.append(f"{k} = ${len(vals)}")
        vals.append(pending_id)
        row = await conn.fetchrow(
            f"UPDATE bybit_pending_orders SET {', '.join(sets)}, updated_at = NOW() "
            f"WHERE id = ${len(vals)} RETURNING *",
            *vals,
        )
    if not row:
        raise HTTPException(404, "not found")
    return _pending_to_out(dict(row))


@router.get("/equity-curve")
async def equity_curve(days: int = Query(30, ge=1, le=365)):
    """Time-series of cumulative PnL for charting."""
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT closed_at, pnl_usd
            FROM bybit_trade_annotations
            WHERE status = 'closed' AND pnl_usd IS NOT NULL
              AND closed_at >= NOW() - ($1 * INTERVAL '1 day')
            ORDER BY closed_at ASC
            """,
            days,
        )
    cum = 0.0
    points = []
    for r in rows:
        cum += float(r["pnl_usd"] or 0)
        points.append({"t": r["closed_at"].isoformat(), "cumulative_pnl": round(cum, 2), "trade_pnl": float(r["pnl_usd"])})
    return {"points": points, "final_pnl": round(cum, 2), "trades": len(points)}
