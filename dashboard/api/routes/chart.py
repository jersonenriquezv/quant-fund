"""TradingView Charting Library Datafeed endpoints.

Implements the UDF (Universal Data Feed) protocol consumed by the Charting
Library on the `/chart` dashboard route: /config, /symbols (resolveSymbol),
/search (searchSymbols), /history (getBars).

Scope is locked to BTC/ETH (the only pairs with clean deep history). Read-only
on bot data — this router only SELECTs candles; it never writes bot tables or
Redis. See docs/plans/chart-replay-2026-06-01.md.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query

from dashboard.api import queries
from shared.models import Candle
from strategy_service.market_structure import MarketStructureAnalyzer
from strategy_service.order_blocks import OrderBlockDetector
from strategy_service.fvg import FVGDetector

router = APIRouter()

# Detector-replay window: how many bars before `to` to drive the detectors.
# Caps the O(window^2) structure recompute; well above any zone's max age in bars.
DETECTION_WINDOW_BARS = 600

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


# --- detections (bot-detection overlay) --------------------------------

def _rows_to_candles(rows: list[dict], pair: str, timeframe: str) -> list[Candle]:
    return [
        Candle(
            timestamp=r["timestamp"],
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
            volume_quote=float(r["volume_quote"]),
            pair=pair,
            timeframe=timeframe,
            confirmed=True,
        )
        for r in rows
    ]


def _replay_detections(candles: list[Candle], pair: str, timeframe: str) -> dict:
    """Drive OB/FVG detectors bar-by-bar up to the last candle (as-of `to`).

    Fidelity notes (see docs/plans/chart-replay-2026-06-01.md, grill Q2):
    - OB/FVG state (mitigation, retest, fill) depends on call ORDER, so we feed
      candles incrementally instead of one-shotting the whole window.
    - Expiration is driven by the `current_time_ms` PARAMETER (= the bar's own
      timestamp), NOT wall-clock `time.time()`. The detectors never read the
      clock directly, so no monkeypatch is needed — passing bar.timestamp makes
      a zone expire exactly as it would have when the live bot saw that bar.
    - MarketStructureAnalyzer.analyze() is stateless (recomputes per call); only
      OB/FVG carry state, so a single analyzer/OB/FVG instance is reused.

    Returns the zones active as of the final bar.
    """
    structure = MarketStructureAnalyzer()
    ob_detector = OrderBlockDetector()
    fvg_detector = FVGDetector()

    active_obs: list = []
    active_fvgs: list = []

    for i in range(len(candles)):
        visible = candles[: i + 1]
        now_ms = visible[-1].timestamp
        state = structure.analyze(visible, pair, timeframe)
        active_obs = ob_detector.update(
            visible, state.structure_breaks, pair, timeframe, now_ms
        )
        active_fvgs = fvg_detector.update(visible, pair, timeframe, now_ms)

    obs = [
        {
            "type": "order_block",
            "direction": ob.direction,
            "timestamp": ob.timestamp,
            "high": ob.high,
            "low": ob.low,
            "body_high": ob.body_high,
            "body_low": ob.body_low,
            "entry_price": ob.entry_price,
            "mitigated": ob.mitigated,
            "impulse_score": ob.impulse_score,
            "retest_count": ob.retest_count,
        }
        for ob in active_obs
    ]
    fvgs = [
        {
            "type": "fvg",
            "direction": fvg.direction,
            "timestamp": fvg.timestamp,
            "high": fvg.high,
            "low": fvg.low,
            "size_pct": fvg.size_pct,
            "filled_pct": fvg.filled_pct,
            "fully_filled": fvg.fully_filled,
        }
        for fvg in active_fvgs
    ]
    return {"order_blocks": obs, "fvgs": fvgs}


@router.get("/chart/detections")
async def chart_detections(
    symbol: str = Query(...),
    resolution: str = Query(...),
    to: int = Query(..., description="As-of time, Unix seconds (UDF convention)"),
) -> dict:
    """Bot-detection overlay: OB/FVG zones active as of bar `to`.

    Replays the bot's own detectors over the window of bars ending at `to`,
    so the overlay matches what the live bot would have recorded at that time
    (the detector-validation tool). CPU-bound replay runs off the event loop.
    """
    _validate_chart_pair(symbol)
    timeframe = _resolve_timeframe(resolution)
    to_ms = to * 1000

    # Window = the last DETECTION_WINDOW_BARS bars at or before `to`.
    rows = await queries.get_candles_range(
        symbol, timeframe, 0, to_ms, limit=DETECTION_WINDOW_BARS
    )
    if not rows:
        return {"order_blocks": [], "fvgs": [], "as_of": to, "bars": 0}

    candles = _rows_to_candles(rows, symbol, timeframe)
    result = await asyncio.to_thread(
        _replay_detections, candles, symbol, timeframe
    )
    result["as_of"] = to
    result["bars"] = len(candles)
    return result
