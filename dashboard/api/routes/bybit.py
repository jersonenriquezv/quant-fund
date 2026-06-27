"""Bybit manual trade log endpoints.

Read/write annotations, list trades, summary stats, weekly review storage.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from dashboard.api import database as db

router = APIRouter(prefix="/bybit", tags=["bybit"])


def _jsonify_row(r: Any) -> dict[str, Any]:
    """asyncpg Record -> JSON-safe dict (Decimal -> float)."""
    out: dict[str, Any] = {}
    for k, v in dict(r).items():
        out[k] = float(v) if isinstance(v, Decimal) else v
    return out


# Journal v2 closed-vocab enums (frozen — keep in sync with the schema comments in
# data_service/bybit_sync.py and the taxonomy table in the plan doc).
_BIAS = r"^(bullish|bearish|range)$"
_STRUCT_REASON = r"^(HH_HL|LH_LL|range_bound|unclear)$"
_LOCATION_PD = r"^(premium|equilibrium|discount)$"
_LOCATION_QUALITY = r"^(key_level|no_mans_land)$"
_MTF = r"^(confirms|contradicts|neutral)$"
_LTF_TRIGGER = r"^(sweep_reclaim|bos|choch|fvg|order_block|simple_break)$"
_STRUCTURE_TYPE = r"^(continuation|reversal|range)$"
_ENTRY_TYPE = r"^(at_level_limit|confirmation_shift)$"
TECHNICAL_ERROR_TAGS = {
    "misread_structure", "sl_bad_placement", "entered_against_htf",
    "early_no_confirmation", "wrong_invalidation", "chased_extended",
}
BEHAVIORAL_ERROR_TAGS = {
    "outcome_bias", "inconsistent_sizing", "revenge_overtrade",
    "not_in_plan", "widened_sl", "cut_winner_early", "held_loser",
}

# JSONB array columns — dumped to JSON on write, parsed on read.
_JSONB_COLS = {"confluences", "technical_error", "behavioral_error"}


class AnnotationUpdate(BaseModel):
    # legacy free-text + demoted-but-kept fields
    setup_type: str | None = None
    confluences: list[str] | None = None
    confidence: int | None = Field(default=None, ge=1, le=5)
    thesis_pre: str | None = None
    trigger_condition: str | None = None
    thesis_invalidation: str | None = None
    lesson_post: str | None = None
    emotional_state: str | None = None
    grade_self: str | None = Field(default=None, pattern=r"^[ABCDF]$")
    screenshot_url: str | None = None
    topdown_brief_used: bool | None = None
    # v2 PLAN: top-down chain (human label; closed vocab)
    htf_bias_daily: str | None = Field(default=None, pattern=_BIAS)
    htf_bias_4h: str | None = Field(default=None, pattern=_BIAS)
    htf_structure_reason: str | None = Field(default=None, pattern=_STRUCT_REASON)
    location_pd: str | None = Field(default=None, pattern=_LOCATION_PD)
    location_quality: str | None = Field(default=None, pattern=_LOCATION_QUALITY)
    mtf_1h: str | None = Field(default=None, pattern=_MTF)
    ltf_trigger: str | None = Field(default=None, pattern=_LTF_TRIGGER)
    structure_type: str | None = Field(default=None, pattern=_STRUCTURE_TYPE)
    entry_type: str | None = Field(default=None, pattern=_ENTRY_TYPE)
    # v2 PLAN: 5 confluence factors (booleans; tf_aligned_count is generated)
    conf_htf: bool | None = None
    conf_location: bool | None = None
    conf_mtf: bool | None = None
    conf_trigger: bool | None = None
    conf_noconflict: bool | None = None
    # v2 PLAN: intended levels (R unit source)
    planned_entry_price: float | None = None
    planned_sl_price: float | None = None
    planned_tp_price: float | None = None
    risk_pct: float | None = Field(default=None, ge=0, le=100)
    # v2 REVIEW: process diagnosis (the clean-sample label; blank-default honesty layer)
    followed_process: bool | None = None
    technical_error: list[str] | None = None
    behavioral_error: list[str] | None = None

    @field_validator("technical_error")
    @classmethod
    def _check_technical(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            bad = set(v) - TECHNICAL_ERROR_TAGS
            if bad:
                raise ValueError(f"unknown technical_error tags: {sorted(bad)}")
        return v

    @field_validator("behavioral_error")
    @classmethod
    def _check_behavioral(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            bad = set(v) - BEHAVIORAL_ERROR_TAGS
            if bad:
                raise ValueError(f"unknown behavioral_error tags: {sorted(bad)}")
        return v


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
    trigger_condition: str | None
    thesis_invalidation: str | None
    lesson_post: str | None
    emotional_state: str | None
    grade_self: str | None
    screenshot_url: str | None
    topdown_brief_used: bool | None
    context_snapshot: dict[str, Any] | None
    auto_setup_type: str | None
    auto_confluences: list[str] | None
    auto_detractors: list[str] | None
    auto_grade: str | None
    auto_classifier_version: int | None
    closed_at: datetime | None
    exit_price: float | None
    pnl_usd: float | None
    pnl_pct: float | None
    status: str
    annotated_at: datetime | None
    # v2 journal — present once the v2 watcher / Phase 3 classifier columns exist.
    journal_schema_version: int | None = None
    # human top-down chain
    htf_bias_daily: str | None = None
    htf_bias_4h: str | None = None
    htf_structure_reason: str | None = None
    location_pd: str | None = None
    location_quality: str | None = None
    mtf_1h: str | None = None
    ltf_trigger: str | None = None
    structure_type: str | None = None
    entry_type: str | None = None
    conf_htf: bool | None = None
    conf_location: bool | None = None
    conf_mtf: bool | None = None
    conf_trigger: bool | None = None
    conf_noconflict: bool | None = None
    tf_aligned_count: int | None = None
    planned_entry_price: float | None = None
    planned_sl_price: float | None = None
    planned_tp_price: float | None = None
    risk_pct: float | None = None
    account_equity_at_open: float | None = None
    position_sl_price: float | None = None
    # machine top-down chain (Phase 3 auto_* — pre-fill source; may diverge from human)
    auto_htf_bias_daily: str | None = None
    auto_htf_bias_4h: str | None = None
    auto_htf_structure_reason: str | None = None
    auto_location_pd: str | None = None
    auto_location_quality: str | None = None
    auto_mtf_1h: str | None = None
    auto_ltf_trigger: str | None = None
    auto_structure_type: str | None = None
    auto_conf_htf: bool | None = None
    auto_conf_location: bool | None = None
    auto_conf_mtf: bool | None = None
    auto_conf_trigger: bool | None = None
    auto_conf_noconflict: bool | None = None
    # REVIEW
    followed_process: bool | None = None
    technical_error: list[str] | None = None
    behavioral_error: list[str] | None = None
    clean_sample: bool | None = None
    trade_quality: str | None = None
    # excursion + R metrics (Phase 4 backfill)
    mae_r: float | None = None
    mfe_r: float | None = None
    realized_r: float | None = None
    exit_efficiency: float | None = None
    entry_slippage_bps: float | None = None
    mae_mfe_tf: str | None = None


def _maybe_json(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return val


def _row_to_out(r: dict) -> AnnotationOut:
    confluences = _maybe_json(r.get("confluences"))
    context = _maybe_json(r.get("context_snapshot"))
    auto_conflu = _maybe_json(r.get("auto_confluences"))
    auto_detr = _maybe_json(r.get("auto_detractors"))
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
        trigger_condition=r.get("trigger_condition"),
        thesis_invalidation=r.get("thesis_invalidation"),
        lesson_post=r.get("lesson_post"),
        emotional_state=r.get("emotional_state"),
        grade_self=r.get("grade_self"),
        screenshot_url=r.get("screenshot_url"),
        topdown_brief_used=r.get("topdown_brief_used"),
        context_snapshot=context,
        auto_setup_type=r.get("auto_setup_type"),
        auto_confluences=auto_conflu,
        auto_detractors=auto_detr,
        auto_grade=r.get("auto_grade"),
        auto_classifier_version=r.get("auto_classifier_version"),
        closed_at=r.get("closed_at"),
        exit_price=r.get("exit_price"),
        pnl_usd=r.get("pnl_usd"),
        pnl_pct=r.get("pnl_pct"),
        status=r.get("status") or "open",
        annotated_at=r.get("annotated_at"),
        journal_schema_version=r.get("journal_schema_version"),
        htf_bias_daily=r.get("htf_bias_daily"),
        htf_bias_4h=r.get("htf_bias_4h"),
        htf_structure_reason=r.get("htf_structure_reason"),
        location_pd=r.get("location_pd"),
        location_quality=r.get("location_quality"),
        mtf_1h=r.get("mtf_1h"),
        ltf_trigger=r.get("ltf_trigger"),
        structure_type=r.get("structure_type"),
        entry_type=r.get("entry_type"),
        conf_htf=r.get("conf_htf"),
        conf_location=r.get("conf_location"),
        conf_mtf=r.get("conf_mtf"),
        conf_trigger=r.get("conf_trigger"),
        conf_noconflict=r.get("conf_noconflict"),
        tf_aligned_count=r.get("tf_aligned_count"),
        planned_entry_price=r.get("planned_entry_price"),
        planned_sl_price=r.get("planned_sl_price"),
        planned_tp_price=r.get("planned_tp_price"),
        risk_pct=r.get("risk_pct"),
        account_equity_at_open=r.get("account_equity_at_open"),
        position_sl_price=r.get("position_sl_price"),
        auto_htf_bias_daily=r.get("auto_htf_bias_daily"),
        auto_htf_bias_4h=r.get("auto_htf_bias_4h"),
        auto_htf_structure_reason=r.get("auto_htf_structure_reason"),
        auto_location_pd=r.get("auto_location_pd"),
        auto_location_quality=r.get("auto_location_quality"),
        auto_mtf_1h=r.get("auto_mtf_1h"),
        auto_ltf_trigger=r.get("auto_ltf_trigger"),
        auto_structure_type=r.get("auto_structure_type"),
        auto_conf_htf=r.get("auto_conf_htf"),
        auto_conf_location=r.get("auto_conf_location"),
        auto_conf_mtf=r.get("auto_conf_mtf"),
        auto_conf_trigger=r.get("auto_conf_trigger"),
        auto_conf_noconflict=r.get("auto_conf_noconflict"),
        followed_process=r.get("followed_process"),
        technical_error=_maybe_json(r.get("technical_error")),
        behavioral_error=_maybe_json(r.get("behavioral_error")),
        clean_sample=r.get("clean_sample"),
        trade_quality=r.get("trade_quality"),
        mae_r=r.get("mae_r"),
        mfe_r=r.get("mfe_r"),
        realized_r=r.get("realized_r"),
        exit_efficiency=r.get("exit_efficiency"),
        entry_slippage_bps=r.get("entry_slippage_bps"),
        mae_mfe_tf=r.get("mae_mfe_tf"),
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
            vals.append(json.dumps(v) if k in _JSONB_COLS and v is not None else v)
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
    # reduce_only orders are SL/TP exits of an open position, not pending entries.
    # Excluding them keeps this panel to genuine entry orders awaiting fill and
    # avoids mislabeling a short's reduce-only Buy exit as a LONG entry.
    async with db.pg_pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM bybit_pending_orders WHERE status = $1 AND reduce_only IS NOT TRUE "
                "ORDER BY placed_at DESC LIMIT $2",
                status, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM bybit_pending_orders WHERE reduce_only IS NOT TRUE "
                "ORDER BY placed_at DESC LIMIT $1",
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


# Static tag descriptions — keep in sync with strategy_service/trade_classifier.py.
# Order matches CLAUDE.md/SYSTEM_BASELINE §10. When the classifier version bumps,
# update both this map and the SYSTEM_BASELINE doc.
GRADE_TAG_DESCRIPTIONS: dict[str, str] = {
    # Confluences
    "htf_4h_aligned": "4H trend aligned with trade direction.",
    "htf_1h_aligned": "1H trend aligned with trade direction.",
    "sweep_recent": "Aligned liquidity sweep within the last 12 hours.",
    "sweep_institutional": "Sweep where the swept level was touched ≥3 times (heavier liquidity).",
    "break_strong_displacement": "Aligned structure break with displacement ≥0.3% (impulsive).",
    "cvd_1h_aligned": "1H CVD (cumulative volume delta) sign matches the trade direction.",
    "funding_neutral": "Funding rate within ±0.03% — no crowd skew either way.",
    "oi_not_crowded": "Open interest 1h change <2% — positioning not stretched.",
    "liq_cluster_magnet": "Nearest liquidation cluster is <3% away — natural magnet.",
    "inside_value_area": "Price inside the 4H value area — mean-reverting context.",
    "at_hvn": "Within 0.5% of a high-volume node — strong support/resistance.",
    "volume_absorption": "Last 5m: vol ≥2× avg with rejection wick (sellers/buyers absorbed).",
    "volume_displacement": "Last 5m: vol ≥2× avg with impulsive body (decisive move).",
    "orderbook_bid_heavy": "Top-20 orderbook bid imbalance ≥0.15 (buyers stacked).",
    "orderbook_ask_heavy": "Top-20 orderbook ask imbalance ≥0.15 (sellers stacked).",
    "adx_trending_aligned": "ADX(14) ≥25 with DI direction matching the trade.",
    # Detractors
    "counter_htf_4h": "4H trend opposes the trade direction — counter-HTF.",
    "cvd_1h_against": "1H CVD opposes the trade direction.",
    "extended_above_vah": "Long entering above the 4H value area high — late.",
    "extended_below_val": "Short entering below the 4H value area low — late.",
}


def _explain_tag(tag: str) -> str:
    """Resolve static + parametric tag descriptions (OB_4h_in_zone, RSI div, etc)."""
    if tag in GRADE_TAG_DESCRIPTIONS:
        return GRADE_TAG_DESCRIPTIONS[tag]
    # Parametric: OB_{tf}_in_zone, OB_{tf}_near, FVG_{tf}_*, BOS_{tf}, CHoCH_{tf},
    # rsi_divergence_{kind}, stoch_rsi_cross_{dir}, funding_extreme_against_{side},
    # oi_{longs|shorts}_crowded, ml_{flag}
    if tag.startswith("OB_"):
        parts = tag.split("_")
        tf = parts[1] if len(parts) > 1 else "?"
        state = "price inside" if tag.endswith("in_zone") else "price ≤1% away from"
        return f"Aligned {tf} order block — {state} the block."
    if tag.startswith("FVG_"):
        parts = tag.split("_")
        tf = parts[1] if len(parts) > 1 else "?"
        state = "price inside" if tag.endswith("in_zone") else "price ≤1% away from"
        return f"Aligned {tf} fair-value gap — {state} the gap."
    if tag.startswith("BOS_"):
        return f"Break of structure on {tag[4:]} timeframe, aligned with trade."
    if tag.startswith("CHoCH_"):
        return f"Change of character on {tag[6:]} timeframe, aligned with trade."
    if tag.startswith("rsi_divergence_"):
        return f"RSI {tag.split('_', 2)[2]} divergence detected."
    if tag.startswith("stoch_rsi_cross_"):
        return f"StochRSI %K/%D crossed in the {tag.rsplit('_', 1)[1]} direction."
    if tag.startswith("funding_extreme_against_"):
        side = tag.rsplit("_", 1)[1]
        return f"Funding rate >0.05% against {side} — crowd squeeze risk."
    if tag.startswith("oi_") and tag.endswith("_crowded"):
        side = tag.split("_")[1]
        return f"Open interest 1h change >3% — {side} positioning crowded."
    if tag.startswith("ml_"):
        flag = tag[3:]
        flags = {
            "rsi_weak": "RSI on the wrong side of 50 for the trade direction.",
            "adx_counter": "ADX directional indicator opposes the trade.",
            "stoch_extreme": "StochRSI in the wrong extreme zone for entry.",
        }
        return flags.get(flag, f"Momentum flag: {flag}.")
    return tag.replace("_", " ")


@router.get("/grade-explain/{annotation_id}")
async def grade_explain(annotation_id: int):
    """Human-readable breakdown of auto-classifier output for one annotation."""
    async with db.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT auto_setup_type, auto_confluences, auto_detractors, auto_grade,
                   auto_classifier_version
            FROM bybit_trade_annotations WHERE id = $1
            """,
            annotation_id,
        )
    if not row:
        raise HTTPException(404, f"annotation {annotation_id} not found")
    conflu = _maybe_json(row["auto_confluences"]) or []
    detr = _maybe_json(row["auto_detractors"]) or []
    net = len(conflu) - len(detr)
    return {
        "annotation_id": annotation_id,
        "auto_setup_type": row["auto_setup_type"],
        "auto_grade": row["auto_grade"],
        "classifier_version": row["auto_classifier_version"],
        "net_score": net,
        "grade_thresholds": {"A": ">=6", "B": ">=4", "C": ">=2", "D": "<2"},
        "confluences": [{"tag": t, "description": _explain_tag(t)} for t in conflu],
        "detractors": [{"tag": t, "description": _explain_tag(t)} for t in detr],
    }


@router.get("/grade-stats")
async def grade_stats(days: int = Query(90, ge=1, le=365)):
    """Per-grade performance aggregate over last N days. Closed trades only."""
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT auto_grade,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE pnl_usd > 0) AS wins,
                   COUNT(*) FILTER (WHERE pnl_usd < 0) AS losses,
                   COALESCE(SUM(pnl_usd) FILTER (WHERE pnl_usd > 0), 0) AS gross_win,
                   COALESCE(SUM(pnl_usd) FILTER (WHERE pnl_usd < 0), 0) AS gross_loss,
                   COALESCE(AVG(pnl_usd), 0) AS avg_pnl,
                   COALESCE(AVG(pnl_pct), 0) AS avg_pnl_pct,
                   COALESCE(SUM(pnl_usd), 0) AS total_pnl
            FROM bybit_trade_annotations
            WHERE status = 'closed' AND auto_grade IS NOT NULL
              AND closed_at >= NOW() - ($1 * INTERVAL '1 day')
            GROUP BY auto_grade
            ORDER BY auto_grade
            """,
            days,
        )
    out = []
    for r in rows:
        d = dict(r)
        n = d["n"] or 0
        wins = d["wins"] or 0
        gross_loss_abs = abs(float(d["gross_loss"] or 0)) or 1e-9
        out.append({
            "auto_grade": d["auto_grade"],
            "n": n,
            "wins": wins,
            "losses": d["losses"] or 0,
            "win_rate_pct": round(wins / n * 100, 1) if n else None,
            "profit_factor": round(float(d["gross_win"] or 0) / gross_loss_abs, 2) if n else None,
            "avg_pnl_usd": round(float(d["avg_pnl"] or 0), 2),
            "avg_pnl_pct": round(float(d["avg_pnl_pct"] or 0), 2),
            "total_pnl_usd": round(float(d["total_pnl"] or 0), 2),
        })
    return {"days": days, "by_grade": out}


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


# Journal v2 base predicate — closed v2 rows in the window. Shared by every v2-stats
# query so legacy v1 rows (frozen, journal_schema_version=1) never contaminate edge math.
_V2_BASE = (
    "FROM bybit_trade_annotations "
    "WHERE journal_schema_version = 2 AND status = 'closed' "
    "AND closed_at >= NOW() - ($1 * INTERVAL '1 day')"
)


@router.get("/v2-stats")
async def v2_stats(days: int = Query(180, ge=1, le=730)):
    """Journal v2 edge + discipline aggregates. Closed v2 rows only.

    `n` is always the first metric. Edge math (expectancy / PF / exit-efficiency)
    filters on `clean_sample` so rule-break trades don't poison the signal;
    `clean_vs_dirty` deliberately compares both to price the cost of breaking rules.
    Empty arrays are normal until v2 trades close + get reviewed.
    """
    async with db.pg_pool.acquire() as conn:
        # A. Expectancy + PF per setup (clean samples only), n first.
        by_setup = await conn.fetch(
            f"""
            SELECT COALESCE(ltf_trigger, '?') AS ltf_trigger,
                   COALESCE(structure_type, '?') AS structure_type,
                   COUNT(*) AS n,
                   ROUND(AVG(realized_r)::numeric, 3) AS expectancy_r,
                   ROUND((100.0 * AVG((realized_r > 0)::int))::numeric, 1) AS win_rate_pct,
                   ROUND((SUM(CASE WHEN realized_r > 0 THEN realized_r ELSE 0 END) /
                          NULLIF(ABS(SUM(CASE WHEN realized_r < 0 THEN realized_r ELSE 0 END)), 0))::numeric, 2)
                       AS profit_factor
            {_V2_BASE} AND clean_sample AND realized_r IS NOT NULL
            GROUP BY ltf_trigger, structure_type
            ORDER BY n DESC
            """,
            days,
        )
        # B. Cost of breaking rules — clean vs dirty (NOT clean_sample-filtered, on purpose).
        clean_dirty = await conn.fetch(
            f"""
            SELECT clean_sample,
                   COUNT(*) AS n,
                   ROUND(AVG(realized_r)::numeric, 3) AS expectancy_r,
                   ROUND(SUM(pnl_usd)::numeric, 2) AS net_usd
            {_V2_BASE}
            GROUP BY clean_sample
            ORDER BY clean_sample NULLS LAST
            """,
            days,
        )
        # C. Behavioral leak ranked (unnest multi-tag), most negative first.
        leaks = await conn.fetch(
            """
            SELECT tag, COUNT(*) AS n, ROUND(SUM(pnl_usd)::numeric, 2) AS net_usd
            FROM bybit_trade_annotations, jsonb_array_elements_text(behavioral_error) tag
            WHERE journal_schema_version = 2 AND status = 'closed'
              AND closed_at >= NOW() - ($1 * INTERVAL '1 day')
            GROUP BY tag
            ORDER BY net_usd ASC
            """,
            days,
        )
        # E. Exit efficiency (cut winners / held losers) per trigger.
        exit_eff = await conn.fetch(
            f"""
            SELECT COALESCE(ltf_trigger, '?') AS ltf_trigger,
                   COUNT(*) AS n,
                   ROUND(AVG(mfe_r)::numeric, 2) AS avg_mfe,
                   ROUND(AVG(mae_r)::numeric, 2) AS avg_mae,
                   ROUND(AVG(realized_r / NULLIF(mfe_r, 0))::numeric, 2) AS exit_eff
            {_V2_BASE} AND clean_sample AND mfe_r > 0
            GROUP BY ltf_trigger
            ORDER BY n DESC
            """,
            days,
        )
        totals = await conn.fetchrow(
            f"""
            SELECT COUNT(*) AS n_closed,
                   COUNT(*) FILTER (WHERE clean_sample) AS n_clean,
                   COUNT(*) FILTER (WHERE followed_process IS NULL) AS n_unreviewed,
                   COUNT(*) FILTER (WHERE realized_r IS NOT NULL) AS n_with_r,
                   ROUND(AVG(realized_r) FILTER (WHERE clean_sample)::numeric, 3)
                       AS clean_expectancy_r
            {_V2_BASE}
            """,
            days,
        )
    return {
        "days": days,
        "totals": _jsonify_row(totals) if totals else {},
        "expectancy_by_setup": [_jsonify_row(r) for r in by_setup],
        "clean_vs_dirty": [_jsonify_row(r) for r in clean_dirty],
        "behavioral_leak": [_jsonify_row(r) for r in leaks],
        "exit_efficiency": [_jsonify_row(r) for r in exit_eff],
    }
