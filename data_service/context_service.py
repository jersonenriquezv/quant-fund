"""Trade context snapshot — pre/post-entry data for manual Bybit trades.

Gathers HTF bias, funding, OI delta, CVD, nearest liquidation cluster, volume
profile context directly from Postgres (populated by the bot's data pipeline).

Called by bybit_watcher on position open. Result stored as JSONB in
bybit_trade_annotations.context_snapshot for later review + Claude analysis.

Input: Bybit symbol like "ETHUSDT" → bot pair like "ETH/USDT".
Output: dict with keys matching watcher format + any extras.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle
from shared.ml_features import (
    _compute_rsi,
    _compute_adx,
    _compute_bollinger,
    _compute_stoch_rsi,
    _detect_rsi_divergence,
    _avg_body_ratio,
    _get_daily_vol,
)
from strategy_service.market_structure import MarketStructureAnalyzer
from strategy_service.order_blocks import OrderBlockDetector
from strategy_service.fvg import FVGDetector
from strategy_service.liquidity import LiquidityAnalyzer
from strategy_service.volume_profile import VolumeProfileAnalyzer

logger = setup_logger("context_service")

CONTEXT_CLASSIFIER_VERSION = 1


def bybit_symbol_to_pair(symbol: str) -> str | None:
    """Convert Bybit 'ETHUSDT' → bot 'ETH/USDT'."""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    if symbol.endswith("USDC"):
        return f"{symbol[:-4]}/USDC"
    if symbol.endswith("USD"):
        return f"{symbol[:-3]}/USD"
    return None


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _htf_bias(pair: str) -> dict[str, Any]:
    """Simple HTF bias from last 20 candles of 4H + 1H.

    bias = 'bullish' if close > EMA20, 'bearish' if <, 'undefined' if within 0.3%.
    """
    out: dict[str, Any] = {}
    with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        for tf, key in [("4h", "bias_4h"), ("1h", "bias_1h")]:
            cur.execute(
                """
                SELECT close FROM candles
                WHERE pair = %s AND timeframe = %s
                ORDER BY timestamp DESC LIMIT 20
                """,
                (pair, tf),
            )
            closes = [float(r["close"]) for r in cur.fetchall()]
            if len(closes) < 10:
                out[key] = "undefined"
                continue
            closes = list(reversed(closes))  # oldest → newest
            ema = closes[0]
            k = 2 / (20 + 1)
            for c in closes[1:]:
                ema = c * k + ema * (1 - k)
            last = closes[-1]
            pct = (last - ema) / ema * 100
            if pct > 0.3:
                out[key] = "bullish"
            elif pct < -0.3:
                out[key] = "bearish"
            else:
                out[key] = "undefined"
    return out


def _funding(pair: str) -> float | None:
    with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT rate FROM funding_rate_history
            WHERE pair = %s ORDER BY timestamp DESC LIMIT 1
            """,
            (pair,),
        )
        row = cur.fetchone()
    return float(row["rate"]) * 100 if row else None  # to pct


def _oi_delta(pair: str, hours: int = 1) -> float | None:
    """OI % change over `hours`."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - hours * 3600 * 1000
    with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT oi_usd FROM open_interest_history
            WHERE pair = %s ORDER BY timestamp DESC LIMIT 1
            """,
            (pair,),
        )
        curr_row = cur.fetchone()
        cur.execute(
            """
            SELECT oi_usd FROM open_interest_history
            WHERE pair = %s AND timestamp <= %s
            ORDER BY timestamp DESC LIMIT 1
            """,
            (pair, start_ms),
        )
        prev_row = cur.fetchone()
    if not curr_row or not prev_row:
        return None
    prev = float(prev_row["oi_usd"])
    curr = float(curr_row["oi_usd"])
    if prev <= 0:
        return None
    return (curr - prev) / prev * 100


def _cvd_summary(pair: str) -> dict[str, Any]:
    with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT cvd_5m, cvd_15m, cvd_1h, buy_volume, sell_volume
            FROM cvd_history
            WHERE pair = %s ORDER BY timestamp DESC LIMIT 1
            """,
            (pair,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    return {
        "cvd_5m": float(row["cvd_5m"]),
        "cvd_15m": float(row["cvd_15m"]),
        "cvd_1h": float(row["cvd_1h"]),
        "buy_vol": float(row["buy_volume"]),
        "sell_vol": float(row["sell_volume"]),
    }


def _nearest_liq_cluster(pair: str, current_price: float) -> dict[str, Any] | None:
    """Approximate nearest liquidation cluster using OI delta + price levels.

    Simplification: no Coinglass integration yet. Estimate from ATR × leverage bands.
    Returns cluster above (short liq) and below (long liq).
    """
    if not current_price:
        return None
    with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT high, low FROM candles
            WHERE pair = %s AND timeframe = '1h'
            ORDER BY timestamp DESC LIMIT 14
            """,
            (pair,),
        )
        rows = cur.fetchall()
    if len(rows) < 5:
        return None
    tr = [float(r["high"]) - float(r["low"]) for r in rows]
    atr = sum(tr) / len(tr)
    # crude: long liq ≈ price - 2×ATR, short liq ≈ price + 2×ATR (pop-zones for 10-20x lev crowd)
    long_liq = current_price - 2 * atr
    short_liq = current_price + 2 * atr
    return {
        "long_liq_approx": round(long_liq, 4),
        "short_liq_approx": round(short_liq, 4),
        "atr_1h": round(atr, 4),
    }


def _fetch_candles(pair: str, tf: str, limit: int = 100) -> list[Candle]:
    """Fetch recent candles as Candle objects (newest last)."""
    with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT timestamp, open, high, low, close, volume, volume_quote
            FROM candles
            WHERE pair = %s AND timeframe = %s
            ORDER BY timestamp DESC LIMIT %s
            """,
            (pair, tf, limit),
        )
        rows = cur.fetchall()
    candles = [
        Candle(
            timestamp=int(r["timestamp"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"] or 0),
            volume_quote=float(r["volume_quote"] or 0),
            pair=pair,
            timeframe=tf,
            confirmed=True,
        )
        for r in rows
    ]
    return list(reversed(candles))  # oldest → newest


def _ml_indicators(pair: str, direction: str) -> dict[str, Any]:
    """Compute the ml_features.py indicator set from 5m candles for bridge-to-ML."""
    out: dict[str, Any] = {}
    candles_5m = _fetch_candles(pair, "5m", limit=200)
    if len(candles_5m) < 30:
        return out

    rsi = _compute_rsi(candles_5m, period=14)
    if rsi is not None:
        out["rsi_14"] = round(rsi, 2)
        out["rsi_zone"] = "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral"
        div = _detect_rsi_divergence(candles_5m, lookback=20)
        out["rsi_divergence"] = div

    adx_result = _compute_adx(candles_5m, period=14)
    if adx_result is not None:
        adx, plus_di, minus_di = adx_result
        out["adx_14"] = round(adx, 2)
        out["plus_di"] = round(plus_di, 2)
        out["minus_di"] = round(minus_di, 2)
        if adx < 20: out["adx_strength"] = "weak"
        elif adx < 25: out["adx_strength"] = "moderate"
        elif adx < 40: out["adx_strength"] = "strong"
        else: out["adx_strength"] = "very_strong"
        out["adx_direction"] = "bullish" if plus_di > minus_di else "bearish"

    bb = _compute_bollinger(candles_5m, period=20, std_mult=2.0)
    if bb is not None:
        width, pct_b, percentile = bb
        out["bb_width_pct"] = round(width, 4) if width is not None else None
        out["bb_percent_b"] = round(pct_b, 4) if pct_b is not None else None
        out["bb_squeeze"] = percentile is not None and percentile < 0.20

    stoch = _compute_stoch_rsi(candles_5m)
    if stoch is not None:
        k, d, prev_k, prev_d = stoch
        out["stoch_rsi_k"] = round(k, 2)
        out["stoch_rsi_d"] = round(d, 2)
        if k <= 20: out["stoch_rsi_zone"] = "oversold"
        elif k >= 80: out["stoch_rsi_zone"] = "overbought"
        else: out["stoch_rsi_zone"] = "neutral"
        if prev_k <= prev_d and k > d: out["stoch_rsi_cross"] = "bullish"
        elif prev_k >= prev_d and k < d: out["stoch_rsi_cross"] = "bearish"

    abr = _avg_body_ratio(candles_5m, n=5)
    if abr is not None:
        out["avg_body_ratio_5"] = round(abr, 3)

    dv = _get_daily_vol(candles_5m, span=100)
    if dv is not None:
        out["daily_vol_ewma"] = round(dv, 5)

    # Direction alignment — are momentum indicators aligned with the trade direction?
    alignments: list[str] = []
    if direction == "long":
        if out.get("rsi_14") and out["rsi_14"] < 50: alignments.append("rsi_weak")
        if out.get("adx_direction") == "bearish": alignments.append("adx_counter")
        if out.get("stoch_rsi_zone") == "overbought": alignments.append("stoch_extreme")
    else:
        if out.get("rsi_14") and out["rsi_14"] > 50: alignments.append("rsi_weak")
        if out.get("adx_direction") == "bullish": alignments.append("adx_counter")
        if out.get("stoch_rsi_zone") == "oversold": alignments.append("stoch_extreme")
    out["momentum_flags"] = alignments
    return out


def _btc_correlation(pair: str, lookback: int = 60) -> float | None:
    """Rolling correlation(close returns) vs BTC/USDT on 5m candles, last `lookback` bars."""
    if pair == "BTC/USDT":
        return 1.0
    pair_candles = _fetch_candles(pair, "5m", limit=lookback + 1)
    btc_candles = _fetch_candles("BTC/USDT", "5m", limit=lookback + 1)
    if len(pair_candles) < 20 or len(btc_candles) < 20:
        return None
    n = min(len(pair_candles), len(btc_candles))
    pair_candles = pair_candles[-n:]
    btc_candles = btc_candles[-n:]
    pair_ret = [pair_candles[i].close / pair_candles[i - 1].close - 1 for i in range(1, n)]
    btc_ret = [btc_candles[i].close / btc_candles[i - 1].close - 1 for i in range(1, n)]
    if len(pair_ret) < 10:
        return None
    m_p = sum(pair_ret) / len(pair_ret)
    m_b = sum(btc_ret) / len(btc_ret)
    num = sum((p - m_p) * (b - m_b) for p, b in zip(pair_ret, btc_ret))
    den_p = (sum((p - m_p) ** 2 for p in pair_ret)) ** 0.5
    den_b = (sum((b - m_b) ** 2 for b in btc_ret)) ** 0.5
    if den_p == 0 or den_b == 0:
        return None
    return round(num / (den_p * den_b), 3)


def _trading_session() -> str:
    """UTC hour → asia/europe/us/overnight."""
    hour = datetime.now(tz=timezone.utc).hour
    if 0 <= hour < 6: return "overnight"
    if 6 <= hour < 13: return "asia"
    if 13 <= hour < 19: return "europe"
    return "us"


def _current_price(pair: str) -> float | None:
    with _conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT close FROM candles
            WHERE pair = %s AND timeframe = '5m'
            ORDER BY timestamp DESC LIMIT 1
            """,
            (pair,),
        )
        row = cur.fetchone()
    return float(row["close"]) if row else None


def _smc_structure(pair: str, direction: str, current_price: float) -> dict[str, Any]:
    """Run SMC detectors (OB, FVG, sweep, BOS/CHoCH) across multiple TFs.

    Returns nearest OB/FVG on each TF, most recent sweep aligned with trade,
    most recent structure break aligned with trade.
    """
    out: dict[str, Any] = {}
    now_ms = int(time.time() * 1000)
    trade_dir_smc = "bullish" if direction == "long" else "bearish"

    timeframes = [("5m", 200), ("15m", 200), ("1h", 200), ("4h", 200)]
    nearest_obs: dict[str, Any] = {}
    nearest_fvgs: dict[str, Any] = {}
    recent_sweeps: list[dict[str, Any]] = []
    recent_breaks: list[dict[str, Any]] = []

    for tf, limit in timeframes:
        candles = _fetch_candles(pair, tf, limit=limit)
        if len(candles) < 2 * settings.SWING_LOOKBACK + 2:
            continue

        ms_analyzer = MarketStructureAnalyzer()
        state = ms_analyzer.analyze(candles, pair, tf)

        ob_detector = OrderBlockDetector()
        obs = ob_detector.update(candles, state.structure_breaks, pair, tf, now_ms)

        fvg_detector = FVGDetector()
        fvgs = fvg_detector.update(candles, pair, tf, now_ms)

        # Nearest OB aligned with trade direction
        dir_obs = [ob for ob in obs if ob.direction == trade_dir_smc and not ob.mitigated]
        if dir_obs and current_price:
            nearest = min(dir_obs, key=lambda o: abs(o.entry_price - current_price))
            dist_pct = (nearest.entry_price - current_price) / current_price * 100
            age_h = (now_ms - nearest.timestamp) / 3_600_000
            nearest_obs[tf] = {
                "entry": round(nearest.entry_price, 6),
                "high": round(nearest.high, 6),
                "low": round(nearest.low, 6),
                "direction": nearest.direction,
                "distance_pct": round(dist_pct, 3),
                "age_hours": round(age_h, 2),
                "volume_ratio": round(nearest.volume_ratio, 2),
                "retest_count": nearest.retest_count,
                "impulse_score": round(nearest.impulse_score, 3),
                "in_zone": nearest.low <= current_price <= nearest.high,
            }

        # Nearest fresh FVG aligned
        dir_fvgs = [f for f in fvgs if f.direction == trade_dir_smc and not f.fully_filled]
        if dir_fvgs and current_price:
            nearest_f = min(dir_fvgs, key=lambda f: abs(((f.high + f.low) / 2) - current_price))
            mid = (nearest_f.high + nearest_f.low) / 2
            dist_pct = (mid - current_price) / current_price * 100
            age_h = (now_ms - nearest_f.timestamp) / 3_600_000
            nearest_fvgs[tf] = {
                "high": round(nearest_f.high, 6),
                "low": round(nearest_f.low, 6),
                "size_pct": round(nearest_f.size_pct * 100, 3),
                "filled_pct": round(nearest_f.filled_pct, 3),
                "distance_pct": round(dist_pct, 3),
                "age_hours": round(age_h, 2),
                "in_zone": nearest_f.low <= current_price <= nearest_f.high,
            }

        # Recent sweep (last ~6h) aligned with trade direction
        liq_analyzer = LiquidityAnalyzer()
        liq_analyzer.update(candles, state.swing_highs, state.swing_lows, pair, tf, None, now_ms)
        sweeps = liq_analyzer.get_recent_sweeps(pair, tf)
        dir_sweeps = [s for s in sweeps if s.direction == trade_dir_smc]
        if dir_sweeps:
            latest = max(dir_sweeps, key=lambda s: s.timestamp)
            age_h = (now_ms - latest.timestamp) / 3_600_000
            if age_h <= 12:
                recent_sweeps.append({
                    "tf": tf,
                    "swept_level": round(latest.swept_level, 6),
                    "wick": round(latest.wick_price, 6),
                    "close": round(latest.close_price, 6),
                    "volume_ratio": round(latest.volume_ratio, 2),
                    "touch_count": latest.swept_level_touch_count,
                    "age_hours": round(age_h, 2),
                })

        # Recent structure break aligned
        if state.latest_break and state.latest_break.direction == trade_dir_smc:
            brk = state.latest_break
            age_h = (now_ms - brk.timestamp) / 3_600_000
            if age_h <= 24:
                disp_pct = abs(brk.break_price - brk.broken_level) / brk.broken_level * 100
                recent_breaks.append({
                    "tf": tf,
                    "type": brk.break_type,  # bos or choch
                    "direction": brk.direction,
                    "broken_level": round(brk.broken_level, 6),
                    "break_price": round(brk.break_price, 6),
                    "displacement_pct": round(disp_pct, 3),
                    "age_hours": round(age_h, 2),
                })

    out["obs_nearest"] = nearest_obs
    out["fvgs_nearest"] = nearest_fvgs
    out["recent_sweeps"] = recent_sweeps
    out["recent_breaks"] = recent_breaks
    return out


def _volume_profile_context(pair: str, current_price: float) -> dict[str, Any]:
    """Compute 4H volume profile over last ~7 days, classify current price zone."""
    candles_4h = _fetch_candles(pair, "4h", limit=60)
    if len(candles_4h) < 20:
        return {}
    analyzer = VolumeProfileAnalyzer()
    profile = analyzer.update(pair, candles_4h)
    if not profile:
        return {}

    if current_price >= profile.vah:
        zone = "above_va"
    elif current_price <= profile.val:
        zone = "below_va"
    else:
        zone = "inside_va"

    dist_poc_pct = (current_price - profile.poc_price) / profile.poc_price * 100 if profile.poc_price else None
    hvn_prices = [p for p, _ in profile.high_volume_nodes[:5]]
    near_hvn = None
    if hvn_prices and current_price:
        closest = min(hvn_prices, key=lambda p: abs(p - current_price))
        near_hvn = {
            "price": round(closest, 6),
            "distance_pct": round((closest - current_price) / current_price * 100, 3),
        }

    return {
        "poc": round(profile.poc_price, 6),
        "vah": round(profile.vah, 6),
        "val": round(profile.val, 6),
        "zone": zone,
        "distance_to_poc_pct": round(dist_poc_pct, 3) if dist_poc_pct is not None else None,
        "near_hvn": near_hvn,
        "hvn_count": len(profile.high_volume_nodes),
        "lvn_count": len(profile.low_volume_nodes),
    }


def _volume_absorption(pair: str) -> dict[str, Any]:
    """Last 5m candle volume vs 20-period average. Detects absorption spikes."""
    candles = _fetch_candles(pair, "5m", limit=21)
    if len(candles) < 20:
        return {}
    last = candles[-1]
    prior = candles[-21:-1] if len(candles) >= 21 else candles[:-1]
    avg_vol = sum(c.volume for c in prior) / len(prior) if prior else 0
    if avg_vol <= 0:
        return {}
    ratio = last.volume / avg_vol
    body = abs(last.close - last.open)
    rng = last.high - last.low
    body_ratio = body / rng if rng > 0 else 0
    direction_last = "bullish" if last.close > last.open else "bearish"
    # Absorption = high volume + small body (rejection) OR high volume + big body (displacement)
    is_absorption = ratio >= 2.0 and body_ratio < 0.35
    is_displacement = ratio >= 2.0 and body_ratio >= 0.60
    return {
        "volume_ratio_5m": round(ratio, 2),
        "body_ratio_5m": round(body_ratio, 3),
        "last_candle_direction": direction_last,
        "absorption_detected": is_absorption,
        "displacement_detected": is_displacement,
    }


def _bybit_orderbook(symbol: str) -> dict[str, Any]:
    """Fetch Bybit L2 orderbook, compute spread, depth and imbalance (top 20)."""
    try:
        import httpx
        url = "https://api.bybit.com/v5/market/orderbook"
        params = {"category": "linear", "symbol": symbol, "limit": 50}
        with httpx.Client(timeout=5.0) as c:
            resp = c.get(url, params=params)
            data = resp.json()
    except Exception as exc:
        logger.warning(f"bybit orderbook failed: {exc}")
        return {}

    result = (data or {}).get("result") or {}
    bids = result.get("b") or []  # [[price, size], ...]
    asks = result.get("a") or []
    if not bids or not asks:
        return {}

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 10000 if mid > 0 else None

    top_n = 20
    bid_depth = sum(float(p) * float(s) for p, s in bids[:top_n])
    ask_depth = sum(float(p) * float(s) for p, s in asks[:top_n])
    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total if total > 0 else 0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
        "bid_depth_usd_top20": round(bid_depth, 2),
        "ask_depth_usd_top20": round(ask_depth, 2),
        "imbalance_top20": round(imbalance, 3),
    }


def build_context_snapshot(symbol: str, side: str) -> dict[str, Any]:
    """Build context snapshot for a Bybit trade.

    side: 'Buy' (long) or 'Sell' (short).
    """
    pair = bybit_symbol_to_pair(symbol)
    if not pair:
        return {"error": f"unknown symbol {symbol}"}

    direction = "long" if side == "Buy" else "short"
    snap: dict[str, Any] = {
        "symbol": symbol,
        "pair": pair,
        "side": side,
        "direction": direction,
        "ts": int(time.time()),
    }

    try:
        htf = _htf_bias(pair)
        bias_4h = htf.get("bias_4h")
        bias_1h = htf.get("bias_1h")
        aligned = None
        if bias_4h in ("bullish", "bearish"):
            aligned = (direction == "long" and bias_4h == "bullish") or (
                direction == "short" and bias_4h == "bearish"
            )
        snap["htf_bias"] = {**htf, "aligned_with_trade": aligned}
    except Exception as exc:
        logger.warning(f"htf_bias failed: {exc}")
        snap["htf_bias"] = {"error": str(exc)}

    try:
        snap["funding"] = _funding(pair)
    except Exception as exc:
        snap["funding"] = None
        logger.warning(f"funding failed: {exc}")

    try:
        snap["oi_delta_1h_pct"] = _oi_delta(pair, hours=1)
        snap["oi_delta_4h_pct"] = _oi_delta(pair, hours=4)
    except Exception as exc:
        logger.warning(f"oi_delta failed: {exc}")

    try:
        snap["cvd"] = _cvd_summary(pair)
    except Exception as exc:
        logger.warning(f"cvd failed: {exc}")
        snap["cvd"] = {}

    try:
        snap["ml_features"] = _ml_indicators(pair, direction)
    except Exception as exc:
        logger.warning(f"ml_indicators failed: {exc}")
        snap["ml_features"] = {}

    try:
        snap["btc_corr_60_5m"] = _btc_correlation(pair, lookback=60)
    except Exception as exc:
        logger.warning(f"btc_corr failed: {exc}")
        snap["btc_corr_60_5m"] = None

    snap["session"] = _trading_session()

    try:
        price = _current_price(pair)
        snap["current_price"] = price
        if price:
            liq = _nearest_liq_cluster(pair, price)
            if liq:
                nearest = liq["short_liq_approx"] if direction == "long" else liq["long_liq_approx"]
                dist = (nearest - price) / price * 100
                snap["nearest_liq_cluster"] = {
                    "side": "shorts" if direction == "long" else "longs",
                    "price": nearest,
                    "distance_pct": dist,
                    "atr_1h": liq["atr_1h"],
                }
    except Exception as exc:
        logger.warning(f"liq cluster failed: {exc}")

    try:
        if snap.get("current_price"):
            snap["smc"] = _smc_structure(pair, direction, snap["current_price"])
    except Exception as exc:
        logger.warning(f"smc_structure failed: {exc}")
        snap["smc"] = {}

    try:
        if snap.get("current_price"):
            snap["volume_profile"] = _volume_profile_context(pair, snap["current_price"])
    except Exception as exc:
        logger.warning(f"volume_profile failed: {exc}")
        snap["volume_profile"] = {}

    try:
        snap["absorption"] = _volume_absorption(pair)
    except Exception as exc:
        logger.warning(f"volume_absorption failed: {exc}")
        snap["absorption"] = {}

    try:
        snap["orderbook"] = _bybit_orderbook(symbol)
    except Exception as exc:
        logger.warning(f"orderbook failed: {exc}")
        snap["orderbook"] = {}

    # Warnings — qualitative flags
    warnings: list[str] = []
    if snap["htf_bias"].get("aligned_with_trade") is False:
        warnings.append("contra 4H trend")
    f = snap.get("funding")
    if f is not None and abs(f) > 0.05:
        warnings.append(f"funding extremo {f:+.3f}%")
    oi1 = snap.get("oi_delta_1h_pct")
    if oi1 is not None:
        if direction == "long" and oi1 > 3.0:
            warnings.append(f"OI 1h +{oi1:.1f}% (longs crowded)")
        elif direction == "short" and oi1 > 3.0:
            warnings.append(f"OI 1h +{oi1:.1f}% (shorts crowded)")
    cvd = snap.get("cvd") or {}
    cvd1h = cvd.get("cvd_1h")
    if cvd1h is not None:
        if direction == "long" and cvd1h < 0:
            warnings.append("CVD 1h negativo (sellers dominando)")
        elif direction == "short" and cvd1h > 0:
            warnings.append("CVD 1h positivo (buyers dominando)")
    ob_best = snap.get("smc", {}).get("obs_nearest", {})
    if ob_best:
        # Flag if no OB is near (<1.5%) on any TF
        min_dist = min(
            (abs(v.get("distance_pct", 999)) for v in ob_best.values() if isinstance(v, dict)),
            default=None,
        )
        if min_dist is not None and min_dist > 1.5:
            warnings.append(f"OB más cercano {min_dist:.1f}% away")

    vp = snap.get("volume_profile") or {}
    if vp.get("zone") == "above_va" and direction == "long":
        warnings.append("longueando sobre VAH (extendido)")
    if vp.get("zone") == "below_va" and direction == "short":
        warnings.append("shorteando bajo VAL (extendido)")

    ob_book = snap.get("orderbook") or {}
    imb = ob_book.get("imbalance_top20")
    if imb is not None:
        if direction == "long" and imb < -0.25:
            warnings.append(f"orderbook ask-heavy ({imb:+.2f})")
        elif direction == "short" and imb > 0.25:
            warnings.append(f"orderbook bid-heavy ({imb:+.2f})")

    snap["warnings"] = warnings
    snap["classifier_version"] = CONTEXT_CLASSIFIER_VERSION
    return snap
