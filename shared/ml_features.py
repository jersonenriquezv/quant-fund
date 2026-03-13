"""
ML feature extraction — structured features for future model training.

Extracts leakage-safe features at setup detection time.
All features are available BEFORE the trade outcome is known.

Two functions:
- extract_setup_features(): strategy-time features (safe for both models)
- extract_risk_context(): portfolio-state features (safe for fill model, caution for quality model)
"""

import re
import time
from typing import Optional

from shared.models import TradeSetup, MarketSnapshot


def extract_setup_features(
    setup: TradeSetup,
    snapshot: Optional[MarketSnapshot],
    current_price: float,
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
    features["cvd_aligned"] = "cvd_aligned" in conf_str
    features["funding_extreme"] = "funding_extreme" in conf_str or "funding_" in conf_str

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
