"""
ML feature extraction — structured features for future model training.

Extracts leakage-safe features at setup detection time.
All features are available BEFORE the trade outcome is known.

Two functions:
- extract_setup_features(): strategy-time features (safe for both models)
- extract_risk_context(): portfolio-state features (safe for fill model, caution for quality model)
"""

import math
import re
import time
from typing import Optional

import numpy as np

from shared.models import TradeSetup, MarketSnapshot


def extract_setup_features(
    setup: TradeSetup,
    snapshot: Optional[MarketSnapshot],
    current_price: float,
    recent_candles: Optional[list] = None,
    *,
    ob_snapshot: Optional[dict] = None,
    btc_candles: Optional[list] = None,
) -> dict:
    """Extract structured features at setup detection time.

    All features are available BEFORE any outcome is known.
    Safe for both fill-probability and trade-quality models.

    Args:
        setup: The detected TradeSetup.
        snapshot: MarketSnapshot at detection time (may be None).
        current_price: Current market price at detection time.

    Returns:
        Flat dict of features for ml_setups table.
    """
    features: dict = {}

    # --- Setup geometry ---
    features["pair"] = setup.pair
    features["direction"] = setup.direction
    features["setup_type"] = setup.setup_type
    features["entry_price"] = setup.entry_price
    features["sl_price"] = setup.sl_price
    features["tp1_price"] = setup.tp1_price
    features["tp2_price"] = setup.tp2_price
    features["htf_bias"] = setup.htf_bias
    features["ob_timeframe"] = setup.ob_timeframe

    # --- Derived geometry ---
    risk = abs(setup.entry_price - setup.sl_price)
    features["risk_distance_pct"] = risk / setup.entry_price if setup.entry_price > 0 else 0
    features["rr_ratio"] = abs(setup.tp2_price - setup.entry_price) / risk if risk > 0 else 0
    # Structural confluence count — only market structure items, not metrics.
    # Metrics (funding, CVD, OI, impulse stats) are captured as separate features.
    _STRUCTURAL_PREFIXES = (
        "liquidity_sweep", "choch", "bos", "order_block", "fvg",
        "breaker_block", "initiating_ob", "bos_confirmed", "pd_zone",
    )
    features["confluence_count"] = sum(
        1 for c in (setup.confluences or [])
        if any(str(c).startswith(p) for p in _STRUCTURAL_PREFIXES)
    )
    features["current_price_at_detection"] = current_price

    # Entry distance — most predictive for fill probability
    if current_price > 0 and setup.entry_price > 0:
        features["entry_distance_pct"] = abs(current_price - setup.entry_price) / current_price
    else:
        features["entry_distance_pct"] = 0

    # SL distance from current price
    if current_price > 0 and setup.sl_price > 0:
        features["sl_distance_pct"] = abs(current_price - setup.sl_price) / current_price
    else:
        features["sl_distance_pct"] = 0

    # --- Stale / late entry features ---
    # Setup age: how old is this setup since the timestamp
    now_ms = int(time.time() * 1000)
    features["setup_age_minutes"] = (now_ms - setup.timestamp) / 60000.0 if setup.timestamp > 0 else 0

    # --- Decomposed confluences ---
    confluences = setup.confluences or []
    conf_str = " ".join(str(c) for c in confluences)

    features["has_liquidity_sweep"] = any("liquidity_sweep" in str(c) for c in confluences)
    features["has_choch"] = any("choch" in str(c) for c in confluences)
    features["has_bos"] = any(
        str(c).startswith("bos_") or str(c) == "bos" for c in confluences
    )
    features["has_fvg"] = any("fvg" in str(c) for c in confluences)
    features["has_breaker_block"] = any("breaker_block" in str(c) for c in confluences)

    # PD zone
    pd_match = re.search(r"pd_zone_(\w+)", conf_str)
    features["pd_zone"] = pd_match.group(1) if pd_match else "undefined"
    features["pd_aligned"] = _is_pd_aligned(features["pd_zone"], setup.direction)

    # OB features from confluences
    features["ob_volume_ratio"] = _extract_float(conf_str, r"ob_volume_([\d.]+)x")
    features["sweep_volume_ratio"] = _extract_float(conf_str, r"sweep_volume_([\d.]+)x")

    # OB depth confirmation (orderbook liquidity at OB zone)
    features["ob_depth_confirmed"] = "ob_depth_confirmed" in conf_str
    features["ob_depth_ratio"] = _extract_float(conf_str, r"ob_depth_ratio_([\d.]+)")
    features["ob_depth_concentration"] = _extract_float(conf_str, r"ob_depth_conc_([\d.]+)")

    # Geometry cascade metadata
    features["geometry_adjusted"] = "geometry_adjusted" in conf_str
    features["geometry_cascade_rank"] = _extract_float(conf_str, r"geometry_adjusted_(\d+)") or 0

    # Volume Profile confluences (v10+)
    features["has_vp_poc"] = "vp_poc_confluence" in conf_str
    features["has_vp_hvn"] = "vp_hvn_confluence" in conf_str
    features["has_vp_lvn"] = "vp_lvn_warning" in conf_str
    features["vp_poc_distance_pct"] = _extract_float(conf_str, r"vp_poc_dist_([\d.]+)")
    if features["vp_poc_distance_pct"]:
        features["vp_poc_distance_pct"] /= 100.0  # Convert to fraction

    features["has_oi_flush"] = "oi_flush" in conf_str
    features["oi_flush_usd"] = _extract_float(conf_str, r"oi_flush_usd_(\d+)")
    features["cvd_aligned"] = "cvd_aligned" in conf_str or "cvd_momentum_confirmed" in conf_str
    features["funding_extreme"] = "funding_extreme" in conf_str

    # --- Graduated signal tiers (v5+) ---
    # Sweep tier
    if "sweep_extreme" in conf_str:
        features["sweep_tier"] = "extreme"
    elif "sweep_strong" in conf_str:
        features["sweep_tier"] = "strong"
    elif "sweep_volume_" in conf_str:
        features["sweep_tier"] = "normal"
    else:
        features["sweep_tier"] = None

    # Funding tier
    if "funding_extreme" in conf_str:
        features["funding_tier"] = "extreme"
    elif "funding_moderate" in conf_str:
        features["funding_tier"] = "moderate"
    elif "funding_mild" in conf_str:
        features["funding_tier"] = "mild"
    else:
        features["funding_tier"] = None

    # OI delta (numeric) — extracted from oi_delta_X.XXpct confluence
    features["oi_delta_pct"] = _extract_float(conf_str, r"oi_delta_(-?[\d.]+)pct")
    if features["oi_delta_pct"] and features["oi_delta_pct"] != 0:
        features["oi_delta_pct"] = features["oi_delta_pct"] / 100.0  # Convert to fraction

    # OI rising tier
    if "oi_rising_strong" in conf_str:
        features["oi_rising_tier"] = "strong"
    elif "oi_rising_moderate" in conf_str:
        features["oi_rising_tier"] = "moderate"
    elif "oi_rising_mild" in conf_str:
        features["oi_rising_tier"] = "mild"
    else:
        features["oi_rising_tier"] = None

    # Buy/sell dominance tier
    if "buy_dominance_strong" in conf_str or "sell_dominance_strong" in conf_str:
        features["dominance_tier"] = "strong"
    elif "buy_dominance_moderate" in conf_str or "sell_dominance_moderate" in conf_str:
        features["dominance_tier"] = "moderate"
    else:
        features["dominance_tier"] = None

    # --- Setup H / momentum-specific features ---
    # Parsed from confluence strings that Setup H adds
    raw_impulse = _extract_float(conf_str, r"impulse_move_([\d.]+)pct")
    features["impulse_move_pct"] = raw_impulse / 100.0 if raw_impulse > 0 else None
    features["impulse_decel_ratio"] = _extract_float(conf_str, r"decel_ratio_([\d.]+)") or None
    features["impulse_vol_decay_ratio"] = _extract_float(conf_str, r"vol_decay_([\d.]+)") or None
    features["impulse_directional_purity"] = _extract_float(conf_str, r"directional_purity_([\d.]+)") or None
    features["has_initiating_ob"] = "initiating_ob" in conf_str

    # --- Market state at detection (from snapshot) ---
    # Funding
    features["has_funding"] = False
    features["funding_rate"] = None
    if snapshot and snapshot.funding and snapshot.funding.rate is not None:
        features["has_funding"] = True
        features["funding_rate"] = snapshot.funding.rate

    # Open interest
    features["has_oi"] = False
    features["oi_usd"] = None
    if snapshot and snapshot.oi and snapshot.oi.oi_usd:
        features["has_oi"] = True
        features["oi_usd"] = snapshot.oi.oi_usd

    # CVD
    features["has_cvd"] = False
    features["cvd_5m"] = None
    features["cvd_15m"] = None
    features["cvd_1h"] = None
    features["buy_dominance"] = None
    if snapshot and snapshot.cvd:
        features["has_cvd"] = True
        features["cvd_5m"] = snapshot.cvd.cvd_5m
        features["cvd_15m"] = snapshot.cvd.cvd_15m
        features["cvd_1h"] = snapshot.cvd.cvd_1h
        total = snapshot.cvd.buy_volume + snapshot.cvd.sell_volume
        if total > 0:
            features["buy_dominance"] = snapshot.cvd.buy_volume / total

    # News sentiment
    features["has_news"] = False
    features["fear_greed_score"] = None
    if snapshot and snapshot.news_sentiment:
        features["has_news"] = True
        features["fear_greed_score"] = snapshot.news_sentiment.score

    # Whale movements
    features["has_whales"] = False
    features["whale_count"] = 0
    if snapshot and snapshot.whale_movements:
        features["has_whales"] = True
        features["whale_count"] = len(snapshot.whale_movements)

    # OI flushes
    features["recent_flush_count"] = 0
    features["recent_flush_total_usd"] = 0
    if snapshot and snapshot.recent_oi_flushes:
        features["recent_flush_count"] = len(snapshot.recent_oi_flushes)
        features["recent_flush_total_usd"] = sum(
            f.size_usd for f in snapshot.recent_oi_flushes
        )

    # --- Temporal / regime features (v5+) ---
    # Hour of day (UTC) — crypto has strong session patterns (Asia/Europe/US)
    features["hour_of_day"] = (now_ms // 3_600_000) % 24

    # ATR as % of price — volatility regime indicator
    # Uses recent candles if provided (20-period ATR)
    features["atr_pct"] = None
    if recent_candles and len(recent_candles) >= 14:
        atr_sum = sum(c.high - c.low for c in recent_candles[-20:])
        atr_count = min(len(recent_candles), 20)
        avg_atr = atr_sum / atr_count
        if current_price > 0:
            features["atr_pct"] = avg_atr / current_price

    # Daily volatility — AFML Ch.3 getDailyVol()
    # EWMA std of close-to-close log-returns, used to normalize barrier widths
    # for ML label analysis. Does NOT change strategy SL/TP (those are structural).
    features["daily_vol"] = _get_daily_vol(recent_candles) if recent_candles else None

    # --- RSI features (v13) ---
    # RSI captures momentum exhaustion — complements structural pattern detection.
    # RSI divergence (price makes new high/low but RSI doesn't) is a leading signal
    # that structural features alone don't capture.
    rsi_val = _compute_rsi(recent_candles, period=14) if recent_candles else None
    features["rsi_14"] = rsi_val
    if rsi_val is not None:
        if rsi_val <= 30:
            features["rsi_zone"] = "oversold"
        elif rsi_val >= 70:
            features["rsi_zone"] = "overbought"
        else:
            features["rsi_zone"] = "neutral"
    else:
        features["rsi_zone"] = None
    features["rsi_divergence"] = _detect_rsi_divergence(
        recent_candles, lookback=20
    ) if recent_candles else None

    # --- Microstructure features (v13) ---
    # Candle body ratio: avg(body/range) of last 5 candles.
    # High = decisive moves (strong trend), low = indecision (dojis/wicks).
    features["avg_body_ratio"] = _avg_body_ratio(recent_candles, n=5) if recent_candles else None

    # --- Orderbook microstructure (v14) ---
    # Spread: cost of entry in basis points. Wide spread = thin book = slippage risk.
    features["spread_bps"] = None
    features["book_imbalance_ratio"] = None
    if ob_snapshot:
        best_bid = ob_snapshot.get("best_bid", 0)
        best_ask = ob_snapshot.get("best_ask", 0)
        if best_bid and best_ask and best_bid > 0:
            features["spread_bps"] = (best_ask - best_bid) / best_bid * 10000
        depth_bid = ob_snapshot.get("depth_bid_usd", 0)
        depth_ask = ob_snapshot.get("depth_ask_usd", 0)
        if depth_bid and depth_ask and depth_ask > 0:
            features["book_imbalance_ratio"] = depth_bid / depth_ask

    # --- BTC correlation (v14) ---
    # For altcoins: how much is BTC moving? If BTC is in impulse, alt setups may be
    # correlation-driven (weaker edge) rather than structural.
    features["btc_return_5"] = None
    features["btc_return_20"] = None
    features["btc_volatility_ratio"] = None
    if btc_candles and len(btc_candles) >= 20 and setup.pair != "BTC/USDT":
        btc_closes = [c.close for c in btc_candles if c.close and c.close > 0]
        if len(btc_closes) >= 20:
            # BTC return over last 5 and 20 candles
            features["btc_return_5"] = (btc_closes[-1] - btc_closes[-5]) / btc_closes[-5]
            features["btc_return_20"] = (btc_closes[-1] - btc_closes[-20]) / btc_closes[-20]
            # BTC volatility vs pair volatility — high ratio = pair moving less than BTC
            btc_atr = sum(c.high - c.low for c in btc_candles[-20:]) / 20
            if current_price > 0 and btc_closes[-1] > 0:
                btc_atr_pct = btc_atr / btc_closes[-1]
                pair_atr_pct = features.get("atr_pct") or 0
                if btc_atr_pct > 0 and pair_atr_pct > 0:
                    features["btc_volatility_ratio"] = pair_atr_pct / btc_atr_pct

    # --- Volatility regime (v14) ---
    # ATR(5) / ATR(50) ratio — >1 = volatility expanding, <1 = contracting.
    # Expansion = breakouts more likely to follow through, contraction = mean reversion.
    features["volatility_regime_ratio"] = None
    if recent_candles and len(recent_candles) >= 50:
        atr_5 = sum(c.high - c.low for c in recent_candles[-5:]) / 5
        atr_50 = sum(c.high - c.low for c in recent_candles[-50:]) / 50
        if atr_50 > 0:
            features["volatility_regime_ratio"] = atr_5 / atr_50

    # --- Trading session (v14) ---
    # Categorical session — more interpretable than raw hour for tree models.
    # Asia (00-08 UTC), Europe (08-14 UTC), US (14-21 UTC), Overlap/Off (21-00 UTC)
    hour = features.get("hour_of_day", 0)
    if hour < 8:
        features["trading_session"] = "asia"
    elif hour < 14:
        features["trading_session"] = "europe"
    elif hour < 21:
        features["trading_session"] = "us"
    else:
        features["trading_session"] = "overlap"

    return features


def extract_risk_context(risk_service) -> dict:
    """Extract risk/portfolio state features at risk check time.

    These features encode recent trade outcomes and portfolio state.
    Safe for fill-probability model. Potentially leaky for quality model
    (risk_daily_dd_pct and risk_weekly_dd_pct encode recent losses).

    Args:
        risk_service: The RiskService instance.

    Returns:
        Dict of risk-context features.
    """
    state = risk_service._state
    return {
        "risk_capital": state.get_capital(),
        "risk_open_positions": state.get_open_positions_count(),
        "risk_daily_dd_pct": state.get_daily_dd_pct(),
        "risk_weekly_dd_pct": state.get_weekly_dd_pct(),
        "risk_trades_today": state.get_trades_today_count(),
    }


# --- Internal helpers ---

def _get_daily_vol(candles: list, span: int = 100) -> float | None:
    """AFML Ch.3 getDailyVol — EWMA std of close-to-close log-returns.

    Adapted for crypto 5m/15m candles: computes log-returns from consecutive
    closes and applies EWMA with configurable span (default 100, ~35-bar
    half-life). Returns the most recent EWMA std value as a fraction of price.

    Args:
        candles: List of Candle objects (oldest first), at least 20.
        span: EWMA span for exponential weighting (default 100 per AFML).

    Returns:
        Daily vol as a float (e.g. 0.02 = 2%), or None if insufficient data.
    """
    if not candles or len(candles) < 20:
        return None

    closes = [c.close for c in candles if c.close and c.close > 0]
    if len(closes) < 20:
        return None

    # Log-returns
    log_returns = []
    for i in range(1, len(closes)):
        lr = math.log(closes[i] / closes[i - 1])
        log_returns.append(lr)

    if len(log_returns) < 10:
        return None

    # EWMA variance (manual — avoids pandas dependency in hot path)
    alpha = 2.0 / (span + 1)
    ewma_var = log_returns[0] ** 2  # seed
    for lr in log_returns[1:]:
        ewma_var = alpha * (lr ** 2) + (1 - alpha) * ewma_var

    return math.sqrt(ewma_var) if ewma_var > 0 else None


def _compute_rsi(candles: list, period: int = 14) -> float | None:
    """Compute RSI(period) from candle closes. Returns 0-100 or None."""
    if not candles or len(candles) < period + 1:
        return None

    closes = [c.close for c in candles if c.close and c.close > 0]
    if len(closes) < period + 1:
        return None

    # Use the last (period+1) closes for a single RSI value,
    # but seed with more data if available for accuracy
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    if len(gains) < period:
        return None

    # Wilder's smoothed RSI (exponential moving average)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _detect_rsi_divergence(candles: list, lookback: int = 20) -> str | None:
    """Detect RSI divergence over the last `lookback` candles.

    Returns: "bullish", "bearish", or None.
    - Bullish divergence: price makes lower low, RSI makes higher low
    - Bearish divergence: price makes higher high, RSI makes lower high
    """
    if not candles or len(candles) < lookback + 14:
        return None

    # Compute RSI for last `lookback` candles
    rsi_values = []
    for i in range(lookback):
        end_idx = len(candles) - lookback + i + 1
        sub = candles[:end_idx]
        r = _compute_rsi(sub, period=14)
        if r is None:
            return None
        rsi_values.append(r)

    if len(rsi_values) < lookback:
        return None

    closes = [c.close for c in candles[-lookback:]]
    mid = lookback // 2

    # Compare first half extreme vs second half extreme
    first_closes = closes[:mid]
    second_closes = closes[mid:]
    first_rsi = rsi_values[:mid]
    second_rsi = rsi_values[mid:]

    # Bullish: price lower low, RSI higher low
    price_lower_low = min(second_closes) < min(first_closes)
    rsi_higher_low = min(second_rsi) > min(first_rsi)
    if price_lower_low and rsi_higher_low:
        return "bullish"

    # Bearish: price higher high, RSI lower high
    price_higher_high = max(second_closes) > max(first_closes)
    rsi_lower_high = max(second_rsi) < max(first_rsi)
    if price_higher_high and rsi_lower_high:
        return "bearish"

    return None


def _avg_body_ratio(candles: list, n: int = 5) -> float | None:
    """Average body/range ratio of last n candles. 1.0=all body, 0.0=all wick."""
    if not candles or len(candles) < n:
        return None

    ratios = []
    for c in candles[-n:]:
        rng = c.high - c.low
        if rng <= 0:
            continue
        body = abs(c.close - c.open)
        ratios.append(body / rng)

    return sum(ratios) / len(ratios) if ratios else None


def _extract_float(text: str, pattern: str) -> float:
    """Extract a float from text using regex. Returns 0.0 if not found."""
    match = re.search(pattern, text)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, IndexError):
            return 0.0
    return 0.0


def _is_pd_aligned(pd_zone: str, direction: str) -> bool:
    """Check if PD zone aligns with trade direction."""
    if pd_zone == "discount" and direction == "long":
        return True
    if pd_zone == "premium" and direction == "short":
        return True
    if pd_zone == "equilibrium":
        return True  # Equilibrium is allowed for both
    return False
