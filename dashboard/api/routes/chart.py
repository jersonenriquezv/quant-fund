"""TradingView Charting Library Datafeed endpoints.

Implements the UDF (Universal Data Feed) protocol consumed by the Charting
Library on the `/chart` dashboard route: /config, /symbols (resolveSymbol),
/search (searchSymbols), /history (getBars).

Scope is locked to BTC/ETH (the only pairs with clean deep history). Read-only
on bot data — this router only SELECTs candles; it never writes bot tables or
Redis. See docs/plans/chart-replay-2026-06-01.md.
"""

from fastapi import APIRouter, HTTPException, Query

from dashboard.api import queries

router = APIRouter()

# --- Scope --------------------------------------------------------------
# Locked to the two pairs with clean, deep candle history (grill decision).
CHART_PAIRS = ("BTC/USDT", "ETH/USDT")

# UDF resolution string -> our DB timeframe label.
RESOLUTION_TO_TIMEFRAME = {
    "5": "5m",
    "15": "15m",
    "60": "1h",
    "240": "4h",
}
SUPPORTED_RESOLUTIONS = list(RESOLUTION_TO_TIMEFRAME.keys())

# Per-pair price formatting for LibrarySymbolInfo.
_PRICESCALE = {
    "BTC/USDT": 100,
    "ETH/USDT": 100,
}


def _validate_chart_pair(symbol: str) -> str:
    """Reject anything outside the BTC/ETH allowlist before hitting the DB."""
    if symbol not in CHART_PAIRS:
        raise HTTPException(400, f"Unsupported chart symbol: {symbol}")
    return symbol


def _resolve_timeframe(resolution: str) -> str:
    tf = RESOLUTION_TO_TIMEFRAME.get(resolution)
    if tf is None:
        raise HTTPException(400, f"Unsupported resolution: {resolution}")
    return tf


@router.get("/chart/config")
async def chart_config() -> dict:
    """DatafeedConfiguration — onReady() payload."""
    return {
        "supported_resolutions": SUPPORTED_RESOLUTIONS,
        "supports_marks": False,
        "supports_timescale_marks": False,
        "supports_time": True,
        "supports_search": True,
        "supports_group_request": False,
        "exchanges": [
            {"value": "OKX", "name": "OKX", "desc": "OKX"},
        ],
        "symbols_types": [
            {"value": "crypto", "name": "Crypto"},
        ],
    }


def _symbol_info(symbol: str) -> dict:
    return {
        "name": symbol,
        "ticker": symbol,
        "description": symbol,
        "type": "crypto",
        "session": "24x7",
        "timezone": "Etc/UTC",
        "exchange": "OKX",
        "listed_exchange": "OKX",
        "minmov": 1,
        "pricescale": _PRICESCALE.get(symbol, 100),
        "has_intraday": True,
        "intraday_multipliers": SUPPORTED_RESOLUTIONS,
        "supported_resolutions": SUPPORTED_RESOLUTIONS,
        "volume_precision": 2,
        "data_status": "streaming",
        "currency_code": "USDT",
    }


@router.get("/chart/symbols")
async def chart_resolve_symbol(symbol: str = Query(...)) -> dict:
    """resolveSymbol — LibrarySymbolInfo for one symbol."""
    _validate_chart_pair(symbol)
    return _symbol_info(symbol)


@router.get("/chart/search")
async def chart_search(
    query: str = Query("", alias="query"),
    type: str = Query(""),
    exchange: str = Query(""),
    limit: int = Query(30, ge=1, le=50),
) -> list[dict]:
    """searchSymbols — restricted to the BTC/ETH allowlist."""
    q = query.upper().strip()
    matches = [s for s in CHART_PAIRS if q in s] if q else list(CHART_PAIRS)
    return [
        {
            "symbol": s,
            "full_name": s,
            "description": s,
            "exchange": "OKX",
            "ticker": s,
            "type": "crypto",
        }
        for s in matches[:limit]
    ]


@router.get("/chart/history")
async def chart_history(
    symbol: str = Query(...),
    resolution: str = Query(...),
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
    countback: int | None = Query(None),
) -> dict:
    """getBars — UDF history response.

    `from`/`to` are Unix SECONDS (UDF convention); our candles store ms.
    Returns {s:"ok", t,o,h,l,c,v} or {s:"no_data", nextTime}.
    """
    _validate_chart_pair(symbol)
    timeframe = _resolve_timeframe(resolution)

    from_ms = from_ * 1000
    to_ms = to * 1000

    rows = await queries.get_candles_range(symbol, timeframe, from_ms, to_ms)

    if not rows:
        # Report the most recent bar before `from` so the library can page back.
        earlier = await queries.get_candles_range(symbol, timeframe, 0, from_ms, limit=1)
        nxt = earlier[-1]["timestamp"] // 1000 if earlier else None
        return {"s": "no_data", "nextTime": nxt}

    return {
        "s": "ok",
        "t": [r["timestamp"] // 1000 for r in rows],
        "o": [float(r["open"]) for r in rows],
        "h": [float(r["high"]) for r in rows],
        "l": [float(r["low"]) for r in rows],
        "c": [float(r["close"]) for r in rows],
        "v": [float(r["volume"]) for r in rows],
    }
