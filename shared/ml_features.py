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
    features["confluence_count"] = len(setup.confluences)
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
