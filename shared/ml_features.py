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

from config.settings import settings
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
    # pd_aligned now strictly means discount-long or premium-short.
    # Previously equilibrium was treated as "aligned for both sides" —
    # that made the feature positive in the most ambiguous zone and
    # diluted predictive power. `pd_zone` categorical still carries the
    # equilibrium signal on its own.
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

    # Funding tier — derived from raw snapshot.funding.rate against
    # FUNDING_MILD/MODERATE/EXTREME thresholds. Direction-agnostic so the ML
    # feature captures crowding magnitude on both sides; the strategy gate
    # (setups.py:_check_volume_confirmation) emits direction-filtered confluence
    # strings for trading decisions, but for ML we want the raw regime signal
    # regardless of trade direction. Audit (W17 2026-04-24) flagged the prior
    # confluence-string parse as 100% null because the gate filters out the
    # majority of cases.
    features["funding_tier"] = _funding_tier_from_rate(
        snapshot.funding.rate if snapshot and snapshot.funding else None
    )

    # OI delta (numeric) — extracted from oi_delta_X.XXpct confluence (always
    # emitted unconditionally by the detector when a prior OI snapshot exists).
    features["oi_delta_pct"] = _extract_float(conf_str, r"oi_delta_(-?[\d.]+)pct")
    if features["oi_delta_pct"] and features["oi_delta_pct"] != 0:
        features["oi_delta_pct"] = features["oi_delta_pct"] / 100.0  # Convert to fraction

    # OI rising tier — derived from extracted oi_delta_pct so the tier matches
    # the raw delta one-to-one. Mirrors detector logic (setups.py:1110) but
    # decoupled from confluence-string emission (which only fires for positive
    # rising deltas, hiding the negative-delta cases from training).
    features["oi_rising_tier"] = _oi_rising_tier_from_delta(features["oi_delta_pct"])

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

    # --- WaveTrend / Cipher B (v15) ---
    # Momentum exhaustion oscillator. Complements SMC structural detection
    # by timing entries within OB/FVG zones when momentum is reversing.
    # wt_cross in extreme zone (|wt1| > 60) aligned with setup direction =
    # momentum confirmation.
    features["wt_wt1"] = None
    features["wt_wt2"] = None
    features["wt_cross"] = None
    features["wt_zone"] = None
    features["wt_aligned"] = None
    if recent_candles:
        wt_result = _compute_wavetrend(recent_candles)
        if wt_result is not None:
            wt1, wt2, prev_wt1, prev_wt2 = wt_result
            features["wt_wt1"] = wt1
            features["wt_wt2"] = wt2
            if prev_wt1 <= prev_wt2 and wt1 > wt2:
                features["wt_cross"] = "bullish"
            elif prev_wt1 >= prev_wt2 and wt1 < wt2:
                features["wt_cross"] = "bearish"
            if wt1 <= -60:
                features["wt_zone"] = "oversold"
            elif wt1 >= 60:
                features["wt_zone"] = "overbought"
            else:
                features["wt_zone"] = "neutral"
            # Alignment: bullish cross + oversold for long, bearish cross + overbought for short
            if setup.direction == "long":
                features["wt_aligned"] = (
                    features["wt_cross"] == "bullish" and wt1 < 0
                )
            elif setup.direction == "short":
                features["wt_aligned"] = (
                    features["wt_cross"] == "bearish" and wt1 > 0
                )

    # --- ADX / DI (v16) ---
    # ADX measures trend STRENGTH. DI+/DI- show direction.
    # Filters setups in choppy ranges (ADX<20) from trending markets (>25).
    features["adx_14"] = None
    features["plus_di_14"] = None
    features["minus_di_14"] = None
    features["adx_trend_strength"] = None
    features["adx_direction"] = None
    if recent_candles:
        adx_result = _compute_adx(recent_candles, period=14)
        if adx_result is not None:
            adx, plus_di, minus_di = adx_result
            features["adx_14"] = adx
            features["plus_di_14"] = plus_di
            features["minus_di_14"] = minus_di
            if adx < 20:
                features["adx_trend_strength"] = "weak"
            elif adx < 25:
                features["adx_trend_strength"] = "moderate"
            elif adx < 40:
                features["adx_trend_strength"] = "strong"
            else:
                features["adx_trend_strength"] = "very_strong"
            features["adx_direction"] = "bullish" if plus_di > minus_di else "bearish"

    # --- Bollinger Bands (v16) ---
    # Volatility compression/expansion + relative price position.
    # bbw_percentile low = squeeze → breakout imminent.
    # percent_b > 1 = above upper band, < 0 = below lower (extreme).
    features["bb_width_pct"] = None
    features["bb_percent_b"] = None
    features["bb_squeeze_percentile"] = None
    features["bb_squeeze"] = None
    if recent_candles:
        bb_result = _compute_bollinger(recent_candles, period=20, std_mult=2.0)
        if bb_result is not None:
            bb_width, percent_b, percentile = bb_result
            features["bb_width_pct"] = bb_width
            features["bb_percent_b"] = percent_b
            features["bb_squeeze_percentile"] = percentile
            features["bb_squeeze"] = percentile is not None and percentile < 0.20

    # --- Stochastic RSI (v16) ---
    # Momentum of RSI — detects reversals faster than raw RSI.
    # Cross of %K above %D in oversold = bullish timing trigger.
    features["stoch_rsi_k"] = None
    features["stoch_rsi_d"] = None
    features["stoch_rsi_zone"] = None
    features["stoch_rsi_cross"] = None
    if recent_candles:
        stoch_result = _compute_stoch_rsi(recent_candles)
        if stoch_result is not None:
            k, d, prev_k, prev_d = stoch_result
            features["stoch_rsi_k"] = k
            features["stoch_rsi_d"] = d
            if k <= 20:
                features["stoch_rsi_zone"] = "oversold"
            elif k >= 80:
                features["stoch_rsi_zone"] = "overbought"
            else:
                features["stoch_rsi_zone"] = "neutral"
            if prev_k <= prev_d and k > d:
                features["stoch_rsi_cross"] = "bullish"
            elif prev_k >= prev_d and k < d:
                features["stoch_rsi_cross"] = "bearish"

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

    # --- Regime label (v18) ---
    # Categorical regime tag derived from existing volatility/trend/squeeze
    # features. Used by the redesign engines (§4.4) as a pre-filter and
    # logged as ML feature for every setup. v1 heuristic — see
    # _compute_regime_label docstring for thresholds and missing-input policy.
    features["regime_label"] = _compute_regime_label(
        atr_ratio=features.get("volatility_regime_ratio"),
        adx_14=features.get("adx_14"),
        bb_squeeze=features.get("bb_squeeze"),
        bb_squeeze_percentile=features.get("bb_squeeze_percentile"),
        btc_return_short=features.get("btc_return_20"),
        spread_bps=features.get("spread_bps"),
        fear_greed=features.get("fear_greed_score"),
    )

    # Engine/benchmark lossless metrics. Prefix-guarded so only known
    # engine namespaces can land in ml_setups, and canonical fields
    # (already populated above) always win to prevent accidental
    # overwrite of pair/setup_type/entry_price/etc.
    extras = getattr(setup, "extra_features", None) or {}
    for key, val in extras.items():
        if not isinstance(key, str):
            continue
        if not any(key.startswith(p) for p in _EXTRA_FEATURE_PREFIXES):
            continue
        if key in features:
            continue
        features[key] = val

    return features


# Whitelist of namespaces allowed to flow from setup.extra_features into
# the ml_setups feature dict. Add new engines/benchmarks here as they
# ship lossless metrics.
_EXTRA_FEATURE_PREFIXES: tuple[str, ...] = (
    "engine1_", "engine2_", "engine3_", "engine4_", "bench_",
)


def extract_risk_context(risk_service, capital_override: float | None = None) -> dict:
    """Extract risk/portfolio state features at risk check time.

    These features encode recent trade outcomes and portfolio state.
    Safe for fill-probability model. Potentially leaky for quality model
    (risk_daily_dd_pct and risk_weekly_dd_pct encode recent losses).

    Args:
        risk_service: The RiskService instance.
        capital_override: If provided, overrides risk_capital. Used by shadow
            mode to align the ml_setups.risk_capital column with SHADOW_CAPITAL
            (virtual sizing) instead of live OKX balance. Keeps analytics and
            shadow_position_size consistent for the same row.

    Returns:
        Dict of risk-context features.
    """
    state = risk_service._state
    capital = capital_override if capital_override is not None else state.get_capital()
    return {
        "risk_capital": capital,
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


def _compute_adx(candles: list, period: int = 14) -> tuple | None:
    """Compute Wilder's ADX(14) + DI+/DI- from candles.

    Returns (adx, plus_di, minus_di) or None if insufficient data.
    ADX measures trend STRENGTH (not direction). DI+/DI- show direction.
    - ADX < 20: weak trend / ranging
    - ADX 20-25: moderate trend
    - ADX > 25: strong trend
    """
    min_len = period * 3  # need warmup for double smoothing
    if not candles or len(candles) < min_len:
        return None

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]

    plus_dm = [0.0]
    minus_dm = [0.0]
    tr = [0.0]
    for i in range(1, len(candles)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr.append(max(hl, hc, lc))

    # Wilder smoothing (seed with sum, then RMA)
    def _wilder(data: list, p: int) -> list:
        if len(data) < p + 1:
            return []
        out = [sum(data[1:p + 1])]  # skip index 0 (seed)
        for i in range(p + 1, len(data)):
            out.append(out[-1] - (out[-1] / p) + data[i])
        return out

    sm_tr = _wilder(tr, period)
    sm_plus = _wilder(plus_dm, period)
    sm_minus = _wilder(minus_dm, period)
    if not sm_tr or len(sm_tr) < period:
        return None

    plus_di_series = [100.0 * sm_plus[i] / sm_tr[i] if sm_tr[i] > 0 else 0.0 for i in range(len(sm_tr))]
    minus_di_series = [100.0 * sm_minus[i] / sm_tr[i] if sm_tr[i] > 0 else 0.0 for i in range(len(sm_tr))]

    dx = []
    for i in range(len(plus_di_series)):
        s = plus_di_series[i] + minus_di_series[i]
        dx.append(100.0 * abs(plus_di_series[i] - minus_di_series[i]) / s if s > 0 else 0.0)

    if len(dx) < period:
        return None

    # ADX = Wilder smoothed DX
    adx = sum(dx[:period]) / period
    for v in dx[period:]:
        adx = (adx * (period - 1) + v) / period

    return (adx, plus_di_series[-1], minus_di_series[-1])


def _compute_bollinger(candles: list, period: int = 20, std_mult: float = 2.0) -> tuple | None:
    """Compute Bollinger Bands width + %B.

    Returns (bb_width_pct, percent_b, bbw_percentile) or None.
    - bb_width_pct: (upper - lower) / middle — volatility proxy
    - percent_b: (close - lower) / (upper - lower) — position in band
    - bbw_percentile: current BBW rank vs last 100 bars (0-1, low = squeeze)
    """
    if not candles or len(candles) < period + 20:
        return None

    closes = [c.close for c in candles if c.close and c.close > 0]
    if len(closes) < period + 20:
        return None

    def _bbw_at(idx: int) -> tuple | None:
        window = closes[idx - period + 1:idx + 1]
        if len(window) < period:
            return None
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        sd = var ** 0.5
        upper = mean + std_mult * sd
        lower = mean - std_mult * sd
        bbw = (upper - lower) / mean if mean > 0 else 0.0
        pb = (closes[idx] - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
        return (bbw, pb)

    curr = _bbw_at(len(closes) - 1)
    if curr is None:
        return None
    bb_width, percent_b = curr

    # BBW percentile rank over last 100 bars
    hist = []
    start = max(period - 1, len(closes) - 100)
    for i in range(start, len(closes)):
        v = _bbw_at(i)
        if v is not None:
            hist.append(v[0])
    if hist:
        below = sum(1 for v in hist if v < bb_width)
        percentile = below / len(hist)
    else:
        percentile = None

    return (bb_width, percent_b, percentile)


def _compute_stoch_rsi(
    candles: list, rsi_period: int = 14, stoch_period: int = 14,
    k_smooth: int = 3, d_smooth: int = 3,
) -> tuple | None:
    """Compute Stochastic RSI (%K, %D) + prior bar for cross detection.

    Returns (k, d, prev_k, prev_d) or None.
    - StochRSI = (RSI - minRSI) / (maxRSI - minRSI) over stoch_period
    - %K = SMA(StochRSI, k_smooth) * 100
    - %D = SMA(%K, d_smooth)
    """
    need = rsi_period + stoch_period + k_smooth + d_smooth + 2
    if not candles or len(candles) < need:
        return None

    # Compute rolling RSI series (one per bar)
    rsi_series = []
    for end in range(rsi_period + 1, len(candles) + 1):
        r = _compute_rsi(candles[:end], period=rsi_period)
        if r is None:
            return None
        rsi_series.append(r)

    if len(rsi_series) < stoch_period + k_smooth + d_smooth:
        return None

    # StochRSI raw
    stoch_raw = []
    for i in range(stoch_period - 1, len(rsi_series)):
        window = rsi_series[i - stoch_period + 1:i + 1]
        mn, mx = min(window), max(window)
        stoch_raw.append((rsi_series[i] - mn) / (mx - mn) if mx > mn else 0.5)

    # %K = SMA(stoch_raw, k_smooth) * 100
    k_series = []
    for i in range(k_smooth - 1, len(stoch_raw)):
        k_series.append(sum(stoch_raw[i - k_smooth + 1:i + 1]) / k_smooth * 100.0)

    if len(k_series) < d_smooth + 1:
        return None

    # %D = SMA(%K, d_smooth)
    d_series = []
    for i in range(d_smooth - 1, len(k_series)):
        d_series.append(sum(k_series[i - d_smooth + 1:i + 1]) / d_smooth)

    if len(d_series) < 2:
        return None

    return (k_series[-1], d_series[-1], k_series[-2], d_series[-2])


def _compute_wavetrend(
    candles: list, n1: int = 10, n2: int = 21
) -> tuple | None:
    """Compute WaveTrend oscillator (Cipher B core) from candles.

    Pine Script reference (LazyBear):
        ap = hlc3
        esa = ema(ap, n1)
        d = ema(abs(ap - esa), n1)
        ci = (ap - esa) / (0.015 * d)
        tci = ema(ci, n2)   // WT1
        wt2 = sma(tci, 4)

    Returns (wt1, wt2, prev_wt1, prev_wt2) — current + prior bar for cross
    detection. None if insufficient data.
    """
    min_len = n1 + n2 + 4
    if not candles or len(candles) < min_len:
        return None

    ap = [(c.high + c.low + c.close) / 3.0 for c in candles if c.close and c.close > 0]
    if len(ap) < min_len:
        return None

    def _ema(data: list, period: int) -> list:
        k = 2.0 / (period + 1)
        out = [data[0]]
        for v in data[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    esa = _ema(ap, n1)
    abs_diff = [abs(ap[i] - esa[i]) for i in range(len(ap))]
    d = _ema(abs_diff, n1)

    ci = []
    for i in range(len(ap)):
        denom = 0.015 * d[i] if d[i] > 0 else 1e-9
        ci.append((ap[i] - esa[i]) / denom)

    wt1_series = _ema(ci, n2)
    if len(wt1_series) < 5:
        return None

    # WT2 = SMA(WT1, 4)
    wt2_curr = sum(wt1_series[-4:]) / 4.0
    wt2_prev = sum(wt1_series[-5:-1]) / 4.0

    return (wt1_series[-1], wt2_curr, wt1_series[-2], wt2_prev)


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
    """Check if PD zone aligns with trade direction.

    Strict alignment: only discount-long and premium-short count.
    Equilibrium is NOT treated as aligned — it is exposed separately as
    feature `pd_zone_equilibrium` so models can learn its effect directly.
    """
    if pd_zone == "discount" and direction == "long":
        return True
    if pd_zone == "premium" and direction == "short":
        return True
    return False


def _funding_tier_from_rate(rate: float | None) -> str | None:
    """Direction-agnostic funding tier from raw rate magnitude.

    Mirrors strategy_service/setups.py:_check_volume_confirmation thresholds
    (FUNDING_MILD/MODERATE/EXTREME), but does not gate on trade direction
    so ML training sees crowding magnitude on every setup. Returns None
    when rate is missing or below the mild threshold.
    """
    if rate is None:
        return None
    abs_rate = abs(rate)
    if abs_rate >= settings.FUNDING_EXTREME_THRESHOLD:
        return "extreme"
    if abs_rate >= settings.FUNDING_MODERATE_THRESHOLD:
        return "moderate"
    if abs_rate >= settings.FUNDING_MILD_THRESHOLD:
        return "mild"
    return None


def _compute_regime_label(
    *,
    atr_ratio: float | None,
    adx_14: float | None,
    bb_squeeze: bool | None,
    bb_squeeze_percentile: float | None,
    btc_return_short: float | None,
    spread_bps: float | None,
    fear_greed: float | None,
) -> str:
    """Categorical market regime tag from existing v17 features.

    v1 heuristic table — NOT optimized. Documented thresholds, not tuned
    parameters. Future calibration runs may revise once an engine has
    enough resolved samples per regime to justify it (see redesign §4.4).

    Inputs are all already extracted in extract_setup_features:
    - atr_ratio: `volatility_regime_ratio` (ATR(5) / ATR(50))
    - adx_14: ADX(14) trend strength
    - bb_squeeze / bb_squeeze_percentile: Bollinger band width regime
    - btc_return_short: 20-bar BTC return on the LTF (proxy for ~60m)
    - spread_bps: orderbook spread in bps
    - fear_greed: Fear & Greed score (0–100)

    Returns one of: trend_strong, trend_weak, range, compression,
    breakout, hostile.

    Missing-input policy (deliberate):
    - Do NOT default to `hostile` on missing inputs — that would
      under-count tradeable regimes when a single feed is stale.
    - `hostile` is reserved for explicit hostile evidence. When the
      signal is partial, fall back to `range` (low ADX) or
      `trend_weak` (mid ADX) as the most conservative tradeable label.
    """
    # --- Hard hostile (explicit evidence only) ---
    if spread_bps is not None and spread_bps > 5.0:
        return "hostile"
    if btc_return_short is not None and abs(btc_return_short) > 0.025:
        return "hostile"
    if fear_greed is not None and fear_greed < 5:
        return "hostile"
    if atr_ratio is not None and (atr_ratio < 0.5 or atr_ratio > 3.0):
        return "hostile"

    # --- Compression: low BB width + low ADX + bounded volatility ---
    is_squeeze = (
        bb_squeeze is True
        or (bb_squeeze_percentile is not None and bb_squeeze_percentile <= 0.20)
    )
    if (
        is_squeeze
        and adx_14 is not None and adx_14 < 20
        and atr_ratio is not None and 0.5 <= atr_ratio <= 1.3
    ):
        return "compression"

    # --- Breakout: ADX rising + volatility expanding ---
    if (
        adx_14 is not None and adx_14 >= 20
        and atr_ratio is not None and atr_ratio >= 1.3
    ):
        return "breakout"

    # --- Trend strong: high ADX + sane volatility band ---
    if (
        adx_14 is not None and adx_14 >= 25
        and atr_ratio is not None and 0.8 <= atr_ratio <= 3.0
    ):
        return "trend_strong"

    # --- Trend weak: moderate ADX, no compression ---
    if adx_14 is not None and 18 <= adx_14 < 25:
        return "trend_weak"

    # --- Range: low ADX, no compression conditions met ---
    if adx_14 is not None and adx_14 < 18:
        return "range"

    # --- Fallback when ADX unavailable ---
    # Conservative: trend_weak when partial data exists (some signal),
    # range when nothing useful is available.
    if atr_ratio is not None or bb_squeeze is not None:
        return "trend_weak"
    return "range"


def _oi_rising_tier_from_delta(oi_delta_pct: float | None) -> str | None:
    """Direction-agnostic OI rising tier from delta-as-fraction.

    Mirrors strategy_service/setups.py:1110 thresholds but works on the
    extracted numeric `oi_delta_pct` (already a fraction at this point in
    extract_setup_features), so dropping deltas (negative) return None
    while strong/moderate/mild positive deltas tier up. The detector also
    emits an `oi_dropping_X` confluence — that is captured by other
    feature columns (`oi_delta_pct` is signed).
    """
    if oi_delta_pct is None or oi_delta_pct <= 0:
        return None
    if oi_delta_pct >= settings.OI_DELTA_STRONG_PCT:
        return "strong"
    if oi_delta_pct >= settings.OI_DELTA_MODERATE_PCT:
        return "moderate"
    if oi_delta_pct >= settings.OI_DELTA_MILD_PCT:
        return "mild"
    return None
