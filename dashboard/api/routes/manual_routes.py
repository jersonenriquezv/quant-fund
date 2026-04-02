"""FastAPI routes for manual trading — calculator, CRUD, analytics."""

import json
import re
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import dashboard.api.database as db
from dashboard.api.manual.calculator import calculate

_PAIR_RE = re.compile(r"^[A-Z0-9]{2,10}/[A-Z]{3,4}$")
from dashboard.api.manual import trade_manager, analytics

router = APIRouter()


# ── Pydantic models ──────────────────────────────────────────────

class CalculateRequest(BaseModel):
    pair: str
    direction: str
    balance: float
    balance_currency: str = "usd"  # "usd" or "coin" (for inverse pairs)
    risk_percent: float = 2.0
    entry: float
    stop_loss: float
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    leverage: int = Field(7, ge=1)
    margin_type: str = "linear"  # "linear" or "inverse"


class CreateTradeRequest(BaseModel):
    pair: str
    direction: str
    balance: float
    balance_currency: str = "usd"  # "usd" or "coin" (for inverse pairs)
    risk_percent: float = 2.0
    entry: float
    stop_loss: float
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    leverage: int = Field(7, ge=1)
    margin_type: str = "linear"
    timeframe: str | None = None
    setup_type: str | None = None
    thesis: str | None = None
    tags: str | None = None
    size_override: float | None = None             # Actual position size (overrides calculated)
    # Structured fundamental data — CoinGlass + Token Terminal (optional, for ML)
    spot_net_flow_4h: float | None = None       # USD, positive=inflow(bearish)
    futures_net_flow_4h: float | None = None    # USD, positive=inflow(bearish)
    cg_ls_ratio: float | None = None            # Long/Short ratio (e.g. 3.0)
    cg_funding_rate: float | None = None        # Weighted funding rate
    fees_trend_wow: float | None = None         # % change in fees WoW
    tvl_delta_7d: float | None = None           # % change in TVL 7d
    upcoming_unlock_usd: float | None = None    # USD unlock in next 14d


class UpdateTradeRequest(BaseModel):
    status: str | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    close_price: float | None = None
    pnl_usd: float | None = None
    result: str | None = None
    thesis: str | None = None
    fundamental_notes: str | None = None
    mistakes: str | None = None
    screenshots: str | None = None
    tags: str | None = None
    timeframe: str | None = None
    setup_type: str | None = None
    created_at: str | None = None
    activated_at: str | None = None
    closed_at: str | None = None
    spot_net_flow_4h: float | None = None
    futures_net_flow_4h: float | None = None
    cg_ls_ratio: float | None = None
    cg_funding_rate: float | None = None
    fees_trend_wow: float | None = None
    tvl_delta_7d: float | None = None
    upcoming_unlock_usd: float | None = None


class PartialCloseRequest(BaseModel):
    close_price: float
    percentage: float = 50.0
    notes: str | None = None


class SetBalanceRequest(BaseModel):
    balance: float


# ── Helpers ─────────────────────────────────────────────────────

async def _get_price(pair: str) -> float | None:
    """Get current price from Redis (maps /USD → /USDT for lookup)."""
    if not db.redis_client:
        return None
    lookup = pair.replace("/USD", "/USDT") if pair.endswith("/USD") else pair
    raw = await db.redis_client.get(f"qf:candle:{lookup}:5m")
    if not raw:
        return None
    candle = json.loads(raw)
    price = candle.get("close")
    return float(price) if price else None


async def _resolve_balance(balance: float, balance_currency: str, pair: str) -> tuple[float, float | None]:
    """Convert coin balance to USD if needed. Returns (balance_usd, coin_price)."""
    if balance_currency != "coin":
        return balance, None
    price = await _get_price(pair)
    if not price:
        raise HTTPException(503, f"Price unavailable for {pair} — cannot convert coin balance")
    return balance * price, price


# ── Calculator ───────────────────────────────────────────────────

@router.post("/manual/calculate")
async def api_calculate(req: CalculateRequest):
    try:
        balance_usd, coin_price = await _resolve_balance(
            req.balance, req.balance_currency, req.pair,
        )
        result = calculate(
            pair=req.pair, direction=req.direction, balance=balance_usd,
            risk_percent=req.risk_percent, entry=req.entry,
            stop_loss=req.stop_loss, take_profit_1=req.take_profit_1,
            take_profit_2=req.take_profit_2, leverage=req.leverage,
            margin_type=req.margin_type,
        )
        data = asdict(result)
        if req.balance_currency == "coin":
            data["coin_balance"] = req.balance
            data["coin_price"] = coin_price
            data["balance_currency"] = "coin"
        return data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/manual/suggested-sl")
async def api_suggested_sl(pair: str, direction: str, entry: float):
    """Find the nearest 4H order block and suggest a SL price.

    For longs: nearest bullish OB below entry → SL = OB low.
    For shorts: nearest bearish OB above entry → SL = OB high.
    """
    if direction not in ("long", "short"):
        raise HTTPException(400, "direction must be 'long' or 'short'")
    if not db.redis_client:
        raise HTTPException(503, "Redis unavailable")

    raw = await db.redis_client.get("qf:bot:order_blocks")
    if not raw:
        return {"suggested_sl": None, "ob": None}

    obs = json.loads(raw)

    # Filter: same pair, 4H timeframe, correct direction
    candidates = []
    for ob in obs:
        if ob["pair"] != pair or ob["timeframe"] != "4h":
            continue

        if direction == "long":
            # Bullish OB below entry = demand zone (support)
            if ob["direction"] == "bullish" and ob["high"] < entry:
                sl_price = ob["low"]
                dist = entry - ob["high"]
                candidates.append((sl_price, dist, ob))
        else:
            # Bearish OB above entry = supply zone (resistance)
            if ob["direction"] == "bearish" and ob["low"] > entry:
                sl_price = ob["high"]
                dist = ob["low"] - entry
                candidates.append((sl_price, dist, ob))

    if not candidates:
        return {"suggested_sl": None, "ob": None}

    # Nearest OB by distance
    candidates.sort(key=lambda x: x[1])
    sl_price, _, ob = candidates[0]

    return {
        "suggested_sl": round(sl_price, 8),
        "ob": {
            "direction": ob["direction"],
            "high": ob["high"],
            "low": ob["low"],
            "body_high": ob["body_high"],
            "body_low": ob["body_low"],
            "entry_price": ob["entry_price"],
            "volume_ratio": ob.get("volume_ratio"),
        },
    }


# ── Trades CRUD ──────────────────────────────────────────────────

@router.post("/manual/trades")
async def api_create_trade(req: CreateTradeRequest):
    balance_usd, _ = await _resolve_balance(
        req.balance, req.balance_currency, req.pair,
    )
    calc = calculate(
        pair=req.pair, direction=req.direction, balance=balance_usd,
        risk_percent=req.risk_percent, entry=req.entry,
        stop_loss=req.stop_loss, take_profit_1=req.take_profit_1,
        take_profit_2=req.take_profit_2, leverage=req.leverage,
        margin_type=req.margin_type,
    )
    data = {
        "pair": req.pair, "direction": req.direction,
        "margin_type": req.margin_type,
        "entry_price": req.entry, "stop_loss": req.stop_loss,
        "take_profit_1": calc.take_profit_1,
        "take_profit_2": calc.take_profit_2,
        "account_balance": balance_usd,
        "risk_percent": req.risk_percent,
        "risk_usd": calc.risk_usd,
        "position_size": req.size_override if req.size_override else calc.position_size,
        "position_value_usd": (req.size_override * req.entry if req.size_override and req.margin_type == "linear"
                               else req.size_override if req.size_override
                               else calc.position_value_usd),
        "leverage": req.leverage,
        "margin_used": ((req.size_override * req.entry / req.leverage) if req.size_override and req.margin_type == "linear"
                        else (req.size_override / (req.entry * req.leverage)) if req.size_override
                        else calc.margin_required),
        "sl_distance_pct": calc.sl_distance_pct,
        "rr_ratio": calc.tp_plan[0].rr_ratio if calc.tp_plan else 0,
        "rr_ratio_tp2": calc.tp_plan[1].rr_ratio if len(calc.tp_plan) > 1 else None,
        "timeframe": req.timeframe,
        "setup_type": req.setup_type,
        "thesis": req.thesis,
        "tags": req.tags,
        "spot_net_flow_4h": req.spot_net_flow_4h,
        "futures_net_flow_4h": req.futures_net_flow_4h,
        "cg_ls_ratio": req.cg_ls_ratio,
        "cg_funding_rate": req.cg_funding_rate,
        "fees_trend_wow": req.fees_trend_wow,
        "tvl_delta_7d": req.tvl_delta_7d,
        "upcoming_unlock_usd": req.upcoming_unlock_usd,
    }
    trade = await trade_manager.create_trade(db.pg_pool, data)
    return trade


@router.get("/manual/trades")
async def api_list_trades(
    status: str | None = None,
    pair: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await trade_manager.get_trades(db.pg_pool, status=status, pair=pair, limit=limit, offset=offset)


@router.get("/manual/trades/{trade_id}")
async def api_get_trade(trade_id: int):
    trade = await trade_manager.get_trade(db.pg_pool, trade_id)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    partials = await trade_manager.get_partial_closes(db.pg_pool, trade_id)
    trade["partial_closes"] = partials
    return trade


@router.patch("/manual/trades/{trade_id}")
async def api_update_trade(trade_id: int, req: UpdateTradeRequest):
    data = req.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")
    trade = await trade_manager.update_trade(db.pg_pool, trade_id, data)
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.delete("/manual/trades/{trade_id}")
async def api_delete_trade(trade_id: int):
    deleted = await trade_manager.delete_trade(db.pg_pool, trade_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"deleted": True}


# ── Partial closes ───────────────────────────────────────────────

@router.post("/manual/trades/{trade_id}/partial-close")
async def api_partial_close(trade_id: int, req: PartialCloseRequest):
    try:
        result = await trade_manager.partial_close(db.pg_pool, trade_id, req.model_dump())
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Balances ─────────────────────────────────────────────────────

@router.get("/manual/balances")
async def api_get_balances():
    return await trade_manager.get_balances(db.pg_pool)


@router.put("/manual/balances/{pair:path}")
async def api_set_balance(pair: str, req: SetBalanceRequest):
    return await trade_manager.set_balance(db.pg_pool, pair, req.balance)


# ── Price (from Redis, for inverse balance conversion) ───────────

@router.get("/manual/price/{pair:path}")
async def api_get_price(pair: str):
    """Get current price for a pair from Redis (bot's cached candle data)."""
    if not _PAIR_RE.match(pair):
        raise HTTPException(400, "Invalid pair format (expected e.g. BTC/USDT)")
    price = await _get_price(pair)
    if not price:
        raise HTTPException(404, f"No price data for {pair}")
    return {"pair": pair, "price": price}


# ── Analytics ────────────────────────────────────────────────────

@router.get("/manual/analytics")
async def api_analytics(
    days: int = Query(30, ge=1, le=365),
    pair: str | None = None,
):
    return await analytics.get_analytics(db.pg_pool, days=days, pair=pair)


# ── HTML page (separate router, no /api prefix) ─────────────────

html_router = APIRouter()


@html_router.get("/manual", response_class=HTMLResponse, include_in_schema=False)
async def manual_page():
    html_path = Path(__file__).resolve().parent.parent / "templates" / "manual.html"
    return HTMLResponse(content=html_path.read_text())
