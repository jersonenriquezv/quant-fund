"""Top-Down Multi-TF Brief — Swing Cascade (Phase 1 tracer).

Read-only analytical tool for manual Bybit entries. Reconciles 4H / 1H / 15m
market structure + order blocks + FVGs + liquidity + volume profile into one
opinionated brief per pair.

Phase 1: console output. 30m slot reserved (TODO line). No Telegram yet.

Run:
  PYTHONPATH=. venv/bin/python scripts/topdown_snapshot.py snapshot BTC/USDT
  PYTHONPATH=. venv/bin/python scripts/topdown_snapshot.py all
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import Optional

import psycopg2

# Backtest time-machine override. When set (via replay_at in
# scripts/backtest_topdown.py), _now_ms() returns this value and _load_candles
# filters candles to those with timestamp <= this value. Production path leaves
# it None — zero behavior change.
_REPLAY_T_MS: Optional[int] = None


def _now_ms() -> int:
    """Wallclock now in ms, or replay override if backtester set it."""
    if _REPLAY_T_MS is not None:
        return _REPLAY_T_MS
    return int(time.time() * 1000)


def _set_replay_time(t_ms: Optional[int]) -> None:
    """Backtester hook: set wallclock override for replay mode.

    Pass None to restore live behavior. Must be paired (set + restore) by the
    caller — _build_snapshot is otherwise unaware of replay mode.
    """
    global _REPLAY_T_MS
    _REPLAY_T_MS = t_ms

from config.settings import settings
from shared.models import Candle
from strategy_service.market_structure import (
    MarketStructureAnalyzer,
    MarketStructureState,
)
from strategy_service.order_blocks import OrderBlockDetector, OrderBlock
from strategy_service.fvg import FVGDetector, FairValueGap
from strategy_service.liquidity import LiquidityAnalyzer, LiquidityLevel
from strategy_service.volume_profile import VolumeProfileAnalyzer, VolumeProfile


PAIRS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT"]
CASCADE_TFS = ["4h", "1h", "30m", "15m"]
# HTF anchors bias — 4H weighted 2x, lower TFs 1x. Total weighted vote = 5.
TF_WEIGHTS = {"4h": 2, "1h": 1, "30m": 1, "15m": 1}
CANDLE_LIMIT_PER_TF = {"4h": 500, "1h": 300, "30m": 300, "15m": 300, "5m": 100, "1d": 60}
NEAR_PRICE_PCT = 0.03  # show zones within 3% of current price
MIN_30M_CANDLES = 200  # backfill if DB has fewer than this for a pair

# ICT Top-Down Analysis vocabulary — every helper below maps 1:1 to a
# documented ICT concept. See docs/topdown_brief_reference.md for sourcing.

# ICT Killzones — exact UTC hour windows per ICT "Killzones" series.
# NOT the same as shared/ml_features.trading_session (those are wide hour
# buckets for ML feature collection). Keep both separate.
ICT_KILLZONES = (
    ("Asian", 20, 24),
    ("London", 2, 5),
    ("NY AM", 12, 15),
    ("NY PM", 18, 20),
)

# Bug fix (SOL incident 2026-05-22): target must be at least this multiple of
# the entry-to-invalidation distance away from the sweep level. Prevents
# noise targets like 84.123 when sweep entry is 84.12.
TARGET_MIN_R_MULTIPLE = 1.5

# Displacement Candle thresholds — ICT Mentorship 2022 "Market Maker Models".
DISPLACEMENT_LOOKBACK_N = 3
DISPLACEMENT_BASELINE_N = 30
DISPLACEMENT_STRONG_RATIO = 2.0
DISPLACEMENT_MODERATE_RATIO = 1.5
DISPLACEMENT_CLOSE_EXTREME_PCT = 0.80

# Inducement (IDM) lookback — scan this many candles BEFORE the last BOS for
# an opposite-direction liquidity sweep. ICT: "IDM precedes the real move".
INDUCEMENT_LOOKBACK_CANDLES = 10

# Freshness flag thresholds — surface staleness in brief footer.
FRESHNESS_OK_MIN = 5      # ≤5 min lag on 5m TF is normal (candle still forming)
FRESHNESS_WARN_MIN = 15   # >15 min lag → ⚠️ STALE flag


@dataclass
class TFAnalysis:
    timeframe: str
    state: MarketStructureState
    obs: list[OrderBlock]
    fvgs: list[FairValueGap]
    liquidity: list[LiquidityLevel]


@dataclass
class Snapshot:
    pair: str
    current_price: float
    current_time_ms: int
    tf_results: dict[str, TFAnalysis]
    vp: Optional[VolumeProfile]
    reconciled_side: str
    confidence: str
    invalidation_level: Optional[float]
    invalidation_reason: str
    # Raw candles per TF — needed by ICT helpers (displacement reads bodies
    # directly, not derived TFAnalysis state). Populated by _build_snapshot.
    raw_candles: dict[str, list[Candle]] = None  # type: ignore[assignment]


def _connect():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _ensure_30m_backfill(cur, conn, pair: str) -> int:
    """Backfill 30m candles into PG if DB has fewer than MIN_30M_CANDLES.

    Returns: count of candles inserted (0 if backfill not needed or failed).
    """
    cur.execute(
        "SELECT COUNT(*) FROM candles WHERE pair = %s AND timeframe = '30m'",
        (pair,),
    )
    count = int(cur.fetchone()[0])
    if count >= MIN_30M_CANDLES:
        return 0
    # Lazy import — only pull ExchangeClient + DataStore when needed
    from data_service.exchange_client import ExchangeClient
    from data_service.data_store import PostgresStore
    client = ExchangeClient()
    candles = client.backfill_candles(pair, "30m", count=MIN_30M_CANDLES + 100)
    if not candles:
        return 0
    store = PostgresStore()
    inserted = store.store_candles(candles)
    return inserted


def _load_candles(cur, pair: str, tf: str, limit: int) -> list[Candle]:
    # In replay mode, hide future candles so snapshot reflects only the state
    # known at _REPLAY_T_MS. Production path (override is None) keeps the
    # original "latest N candles" semantics exactly.
    if _REPLAY_T_MS is not None:
        cur.execute(
            """
            SELECT timestamp, open, high, low, close, volume, volume_quote
            FROM candles
            WHERE pair = %s AND timeframe = %s AND timestamp <= %s
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            (pair, tf, _REPLAY_T_MS, limit),
        )
    else:
        cur.execute(
            """
            SELECT timestamp, open, high, low, close, volume, volume_quote
            FROM candles
            WHERE pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            (pair, tf, limit),
        )
    rows = cur.fetchall()
    return [
        Candle(
            timestamp=int(r[0]), open=float(r[1]), high=float(r[2]),
            low=float(r[3]), close=float(r[4]), volume=float(r[5]),
            volume_quote=float(r[6]) if r[6] is not None else 0.0,
            pair=pair, timeframe=tf, confirmed=True,
        )
        for r in reversed(rows)
    ]


def _analyze_tf(candles: list[Candle], pair: str, tf: str,
                current_time_ms: int) -> TFAnalysis:
    ms_analyzer = MarketStructureAnalyzer()
    ob_detector = OrderBlockDetector()
    fvg_detector = FVGDetector()
    liq_analyzer = LiquidityAnalyzer()

    state = ms_analyzer.analyze(candles, pair, tf)
    obs = ob_detector.update(
        candles, state.structure_breaks, pair, tf, current_time_ms
    )
    fvgs = fvg_detector.update(candles, pair, tf, current_time_ms)
    liq_analyzer.update(
        candles, state.swing_highs, state.swing_lows,
        pair, tf, None, current_time_ms,
    )
    levels = liq_analyzer.get_levels(pair, tf)
    return TFAnalysis(
        timeframe=tf, state=state, obs=obs, fvgs=fvgs, liquidity=levels,
    )


def _reconcile(tf_results: dict[str, TFAnalysis]) -> tuple[str, str]:
    """HTF-weighted reconciliation across the cascade TFs.

    4H weighted 2x, 1H/30m/15m weighted 1x. Total possible = 5.
    Confidence: high = ≥4/5 weighted score, medium = simple majority,
    low = undefined-heavy or HTF disagrees with majority direction.

    Returns (side, confidence).
    """
    bull_score = 0
    bear_score = 0
    undef_count = 0
    total_weight = 0
    htf_trend = None
    for tf in CASCADE_TFS:
        if tf not in tf_results:
            continue
        w = TF_WEIGHTS.get(tf, 1)
        total_weight += w
        trend = tf_results[tf].state.trend
        if tf == "4h":
            htf_trend = trend
        if trend == "bullish":
            bull_score += w
        elif trend == "bearish":
            bear_score += w
        else:
            undef_count += 1

    if undef_count >= 2 or total_weight == 0:
        return "undefined", "low"

    # Strong: weighted score ≥ 4/5 (4H + ≥2 LTFs aligned)
    if bull_score >= 4:
        return "long", "high"
    if bear_score >= 4:
        return "short", "high"

    if bull_score > bear_score:
        # Demote to low if HTF disagrees with majority direction
        if htf_trend == "bearish":
            return "long", "low"
        return "long", "medium"
    if bear_score > bull_score:
        if htf_trend == "bullish":
            return "short", "low"
        return "short", "medium"
    return "undefined", "low"


def _pick_invalidation(
    side: str, current_price: float, tf_results: dict[str, TFAnalysis],
) -> tuple[Optional[float], str]:
    """Invalidation = nearest 4H swing that, if broken, flips the thesis."""
    htf = tf_results.get("4h")
    if htf is None:
        return None, "no 4h data"
    if side == "long":
        lows = [s.price for s in htf.state.swing_lows if s.price < current_price]
        if not lows:
            return None, "no 4h swing low below price"
        level = max(lows)
        return level, "4H close below last swing low"
    if side == "short":
        highs = [s.price for s in htf.state.swing_highs if s.price > current_price]
        if not highs:
            return None, "no 4h swing high above price"
        level = min(highs)
        return level, "4H close above last swing high"
    return None, "side undefined"


# ---------------------------------------------------------------------------
# ICT Helpers — all map to documented ICT Top-Down Analysis vocabulary.
# See docs/topdown_brief_reference.md for sourcing.
# ---------------------------------------------------------------------------


def _displacement_read(candles: list[Candle]) -> dict:
    """ICT Displacement Candle — strength of recent impulse.

    Compares last DISPLACEMENT_LOOKBACK_N candles to a baseline window.
    Strong = bodies ≥2× baseline AND same direction AND close near extreme.

    See ICT Mentorship 2022 "Market Maker Models" — displacement leaves FVG/OB
    behind, marks institutional commitment.

    Returns dict with strength label + diagnostics, or None-equivalent dict
    when insufficient candles.
    """
    if len(candles) < DISPLACEMENT_LOOKBACK_N + DISPLACEMENT_BASELINE_N:
        return {
            "strength": "unknown", "body_ratio": 0.0,
            "direction_consistent": False, "close_to_extreme_pct": 0.0,
            "direction": "neutral",
        }

    recent = candles[-DISPLACEMENT_LOOKBACK_N:]
    baseline = candles[-(DISPLACEMENT_LOOKBACK_N + DISPLACEMENT_BASELINE_N):
                       -DISPLACEMENT_LOOKBACK_N]

    def body_pct(c: Candle) -> float:
        if c.open == 0:
            return 0.0
        return abs(c.close - c.open) / c.open * 100

    recent_avg = sum(body_pct(c) for c in recent) / len(recent)
    baseline_avg = sum(body_pct(c) for c in baseline) / len(baseline)
    body_ratio = recent_avg / baseline_avg if baseline_avg > 0 else 0.0

    bull_count = sum(1 for c in recent if c.close > c.open)
    bear_count = sum(1 for c in recent if c.close < c.open)
    direction_consistent = bull_count == len(recent) or bear_count == len(recent)
    direction = "bull" if bull_count > bear_count else (
        "bear" if bear_count > bull_count else "neutral"
    )

    # Close-to-extreme ratio averaged across recent candles
    def close_to_extreme(c: Candle) -> float:
        rng = c.high - c.low
        if rng <= 0:
            return 0.0
        if c.close > c.open:
            return (c.close - c.low) / rng
        return (c.high - c.close) / rng

    close_to_extreme_pct = sum(close_to_extreme(c) for c in recent) / len(recent)

    if (body_ratio >= DISPLACEMENT_STRONG_RATIO
            and direction_consistent
            and close_to_extreme_pct >= DISPLACEMENT_CLOSE_EXTREME_PCT):
        strength = "strong"
    elif body_ratio >= DISPLACEMENT_MODERATE_RATIO and direction_consistent:
        strength = "moderate"
    else:
        strength = "weak"

    return {
        "strength": strength,
        "body_ratio": round(body_ratio, 2),
        "direction_consistent": direction_consistent,
        "close_to_extreme_pct": round(close_to_extreme_pct, 2),
        "direction": direction,
    }


def _pd_array_position(
    htf_candles: list[Candle],
    htf_state: MarketStructureState,
    pair: str,
    current_price: float,
    current_time_ms: int,
) -> Optional[dict]:
    """ICT PD Array / Dealing Range — thin wrapper over LiquidityAnalyzer.

    Calls update_premium_discount() in strategy_service/liquidity.py and
    computes position_pct. Uses a fresh analyzer per call (no need for
    cached state since we want a fresh read every brief).

    See ICT "Premium and Discount Arrays" — equilibrium at 50%, premium >50%,
    discount <50%. Bias-aligned entries come from premium (shorts) or
    discount (longs).
    """
    analyzer = LiquidityAnalyzer()
    pd_zone = analyzer.update_premium_discount(
        htf_candles=htf_candles,
        htf_swing_highs=htf_state.swing_highs,
        htf_swing_lows=htf_state.swing_lows,
        pair=pair,
        current_price=current_price,
        current_time_ms=current_time_ms,
    )
    if pd_zone is None:
        return None
    rng = pd_zone.range_high - pd_zone.range_low
    if rng <= 0:
        return None
    position_pct = (current_price - pd_zone.range_low) / rng * 100
    return {
        "position_pct": round(position_pct, 1),
        "zone": pd_zone.zone,
        "range_low": pd_zone.range_low,
        "range_high": pd_zone.range_high,
    }


def _inducement_check(htf: TFAnalysis) -> dict:
    """ICT Inducement (IDM) — sweep of opposite-side liquidity before BOS.

    Scans liquidity levels for any swept level within INDUCEMENT_LOOKBACK_CANDLES
    BEFORE the latest BOS timestamp, in opposite direction:
      - bearish BOS → look for swept BSL above (longs got run out)
      - bullish BOS → look for swept SSL below (shorts got run out)

    Returns {has_idm: bool, idm_level: float|None, idm_swept_at: int|None}.

    See ICT direct term "IDM" — institutional bait that precedes the real move.
    """
    latest = htf.state.latest_break
    if latest is None:
        return {"has_idm": False, "idm_level": None, "idm_swept_at": None}

    target_type = "bsl" if latest.direction == "bearish" else "ssl"
    bos_ts = latest.timestamp

    candidates = []
    for lvl in htf.liquidity:
        if lvl.level_type != target_type or not lvl.swept:
            continue
        if not lvl.timestamps:
            continue
        # Use most-recent timestamp in the cluster as the "swept around when"
        # proxy (LiquidityLevel doesn't carry sweep timestamp directly; use
        # the last touch as the institutional formation timestamp).
        last_touch = max(lvl.timestamps)
        if last_touch >= bos_ts:
            continue  # only swept-before-BOS counts as IDM
        candidates.append((last_touch, lvl))

    if not candidates:
        return {"has_idm": False, "idm_level": None, "idm_swept_at": None}

    # Most recent swept opposite-side liquidity before BOS = the IDM
    candidates.sort(key=lambda t: t[0], reverse=True)
    swept_at, lvl = candidates[0]
    return {"has_idm": True, "idm_level": lvl.price, "idm_swept_at": swept_at}


def _killzone_now(timestamp_ms: int) -> dict:
    """ICT Killzones — exact UTC hour windows per ICT "Killzones" series.

    Returns {name, active, next_name, minutes_to_next}. Does NOT reuse
    shared/ml_features.trading_session (those buckets are wider — Asian
    00-07 UTC ≠ ICT Asian 20-00). Kept separate by design.
    """
    seconds = timestamp_ms // 1000
    hour = (seconds // 3600) % 24
    minute = (seconds // 60) % 60

    for name, start, end in ICT_KILLZONES:
        if start <= hour < end:
            return {"name": name, "active": True,
                    "next_name": None, "minutes_to_next": 0}

    # Not currently in any killzone — find nearest upcoming
    minutes_now = hour * 60 + minute
    candidates: list[tuple[int, str]] = []
    for name, start, _end in ICT_KILLZONES:
        start_min = start * 60
        delta = start_min - minutes_now
        if delta <= 0:
            delta += 24 * 60  # next day
        candidates.append((delta, name))
    candidates.sort()
    delta_min, next_name = candidates[0]
    return {"name": None, "active": False,
            "next_name": next_name, "minutes_to_next": delta_min}


# ---------------------------------------------------------------------------
# PR1 v2 helpers — quick wins from user testing 2026-05-23.
# See docs/grill/topdown-v2-context-simplicity-2026-05-23.md.
# ---------------------------------------------------------------------------

# Sweep distance gate — beyond this, brief renders "spectator" instead of play.
# Tightened 2026-05-24 from 5.0 → 1.0 after `backtest_results/topdown_20260524_192804_report.md`
# §5 showed 3-5% bucket WR = 0% / 17 SLs, 1-2% bucket WR = 15.8%, 0-1% bucket WR = 23.6%.
# Below 1% is where the brief's edge actually concentrates. Loses ~80% of emissions but
# raises per-emission quality.
SWEEP_MAX_ACTIONABLE_PCT = 1.0


def _pd_bias_conflict(reconciled_side: str, pd_zone: Optional[str]) -> bool:
    """Detect ICT premium/discount vs reconciled bias contradiction.

    ICT teaching: shorts from PREMIUM (>50% of HTF range), longs from DISCOUNT
    (<50%). When bias is short but price is in discount → counter-PD trade,
    lower quality. Same for long-in-premium. Equilibrium is neutral, no
    conflict either way.
    """
    if reconciled_side not in ("long", "short") or pd_zone in (None, "equilibrium"):
        return False
    if reconciled_side == "short" and pd_zone == "discount":
        return True
    if reconciled_side == "long" and pd_zone == "premium":
        return True
    return False


def _sweep_distance_pct(current_price: float, sweep_level: Optional[float]) -> Optional[float]:
    """Return abs distance from current price to sweep level as percentage.

    None when sweep_level is missing.
    """
    if sweep_level is None or current_price <= 0:
        return None
    return abs(sweep_level - current_price) / current_price * 100


def _sweep_actionable(distance_pct: Optional[float],
                       max_pct: float = SWEEP_MAX_ACTIONABLE_PCT) -> bool:
    """Sweep entry is "actionable" only when within max_pct of current price.

    Beyond that the trade is spectator territory — brief should say wait or skip.
    """
    if distance_pct is None:
        return False
    return distance_pct <= max_pct


def _trade_triplet(snap: Snapshot) -> Optional[dict]:
    """Compute explicit entry / SL / TP / R:R for the current play.

    Entry = nearest aligned unbroken liquidity (BSL for short, SSL for long)
            within actionable sweep distance.
    SL = invalidation_level (4H swing) from Snapshot.
    TP = furthest unbroken liquidity past 1.5R floor from sweep entry.
    Returns None when any leg is missing or sweep is not actionable.
    """
    side = snap.reconciled_side
    price = snap.current_price
    inv = snap.invalidation_level
    if side not in ("long", "short") or inv is None:
        return None

    # Collect unbroken liquidity, sort by proximity to current price
    above_levels = []
    below_levels = []
    for tfa in snap.tf_results.values():
        for lvl in tfa.liquidity:
            if lvl.swept:
                continue
            if lvl.price > price:
                above_levels.append(lvl)
            elif lvl.price < price:
                below_levels.append(lvl)
    above_levels.sort(key=lambda l: l.price)            # nearest first
    below_levels.sort(key=lambda l: l.price, reverse=True)

    sweep = above_levels[0] if side == "short" and above_levels else (
        below_levels[0] if side == "long" and below_levels else None
    )
    if sweep is None:
        return None
    sweep_pct = _sweep_distance_pct(price, sweep.price)
    if not _sweep_actionable(sweep_pct):
        return {"valid": False, "reason": "sweep_too_far",
                "sweep_distance_pct": sweep_pct}

    # Geometry guard: invalidation must be on the protective side of entry.
    # Long: SL below entry (stop deeper, trade dies if liquidity below sweep
    # also fails). Short: SL above entry. Otherwise the "stop" sits between
    # entry and the move direction — the trade self-exits at a profit before
    # the thesis is truly invalidated. Surfaced by Phase 1 tracer 2026-05-24.
    if side == "long" and inv >= sweep.price:
        return {"valid": False, "reason": "sl_wrong_side",
                "entry": sweep.price, "sl": inv, "side": side}
    if side == "short" and inv <= sweep.price:
        return {"valid": False, "reason": "sl_wrong_side",
                "entry": sweep.price, "sl": inv, "side": side}

    # Target picker — reuse 1.5R floor from existing helper
    min_dist = _min_target_distance(sweep.price, inv)
    if side == "short":
        target = _pick_valid_target(below_levels, "short", sweep.price, min_dist)
    else:
        above_for_target = sorted(above_levels, key=lambda l: l.price)
        target = _pick_valid_target(above_for_target, "long", sweep.price, min_dist)

    if target is None:
        return {"valid": False, "reason": "no_valid_target",
                "entry": sweep.price, "sl": inv}

    risk = abs(sweep.price - inv)
    reward = abs(target.price - sweep.price)
    rr = reward / risk if risk > 0 else 0.0

    # PR3: adaptive TP — scaled if target distance ≥ 2× daily ATR(14).
    # Intermediates = unbroken liquidity between entry and final target.
    daily_candles = snap.raw_candles.get("1d", []) if hasattr(snap, "raw_candles") else []
    atr = _daily_atr(daily_candles)
    if side == "short":
        intermediates = [l for l in below_levels
                         if target.price < l.price < sweep.price]
    else:
        intermediates = [l for l in above_levels
                         if sweep.price < l.price < target.price]
    tp_info = _adaptive_tp(sweep.price, inv, target.price, intermediates, atr)

    result = {
        "valid": True,
        "entry": sweep.price,
        "sl": inv,
        "tp": target.price,
        "rr": round(rr, 2),
        "sweep_distance_pct": round(sweep_pct, 2),
        "risk_pct": round(risk / sweep.price * 100, 2),
        "reward_pct": round(reward / sweep.price * 100, 2),
        "tp_mode": tp_info["mode"],
        "tp1": tp_info["tp1"],
        "tp2": tp_info["tp2"],
        "splits": tp_info["splits"],
        "atr": atr,
    }
    if tp_info["mode"] == "scaled":
        rr_tp1 = abs(tp_info["tp1"] - sweep.price) / risk if risk > 0 else 0.0
        rr_tp2 = abs(tp_info["tp2"] - sweep.price) / risk if risk > 0 else 0.0
        result["rr_tp1"] = round(rr_tp1, 2)
        result["rr_tp2"] = round(rr_tp2, 2)
    return result


def _bos_session_quality(latest_break_ts: Optional[int]) -> Optional[dict]:
    """Classify the killzone session of the latest BOS/CHoCH.

    ICT teaching: BOS during Asian killzone often = liquidity grab / inducement,
    lower confidence vs BOS during London or NY. Used to downgrade conviction
    when the structural shift only has Asian-session confirmation.

    Returns {session: str, quality: "high"|"low"} or None when no break.
    """
    if latest_break_ts is None:
        return None
    kz = _killzone_now(latest_break_ts)
    if not kz["active"]:
        return {"session": "dead zone", "quality": "low"}
    session = kz["name"]
    quality = "low" if session == "Asian" else "high"
    return {"session": session, "quality": quality}


# ---------------------------------------------------------------------------
# PR2 v2 — Daily Context Memory (PDH/PDL/PWH/PWL + bias chain + today).
# Pure derivation over candles[1d] already loaded into Snapshot.raw_candles.
# ICT: closes commit, wicks bait. All "taken/swept/broken" reads use CLOSES.
# ---------------------------------------------------------------------------

# Doji tolerance — |close-open|/open below this = doji, not bull/bear.
DAILY_DOJI_TOLERANCE = 0.001
# Bias chain window — last N daily candles surfaced in brief.
DAILY_CHAIN_N = 5


def _compute_pdh_pdl(daily_candles: list[Candle]) -> Optional[dict]:
    """ICT PDH / PDL — Previous Day High / Low + today's status vs them.

    Status per level uses CLOSES, not wicks:
      - "untaken": today_high (so far) has not exceeded the level
      - "swept": today_high > level but today_close < level (wick liar)
      - "broken": today_close > level (commitment)
    Same logic mirrored for PDL with lows / closes below.

    Returns None when <2 daily candles available.
    """
    if len(daily_candles) < 2:
        return None
    prev = daily_candles[-2]
    today = daily_candles[-1]
    pdh = prev.high
    pdl = prev.low

    if today.high > pdh:
        if today.close > pdh:
            pdh_status = "broken"
        else:
            pdh_status = "swept"
    else:
        pdh_status = "untaken"

    if today.low < pdl:
        if today.close < pdl:
            pdl_status = "broken"
        else:
            pdl_status = "swept"
    else:
        pdl_status = "untaken"

    return {
        "pdh": pdh, "pdl": pdl,
        "pdh_status": pdh_status, "pdl_status": pdl_status,
        "today_open": today.open, "today_close_so_far": today.close,
        "today_high": today.high, "today_low": today.low,
    }


def _compute_pwh_pwl(daily_candles: list[Candle]) -> Optional[dict]:
    """ICT PWH / PWL — previous calendar week high / low (Mon 00:00 UTC start).

    Week definition: Monday 00:00 UTC through Sunday 23:59 UTC. ICT also uses
    Sunday-open (futures convention) but Monday-start is calendar-standard and
    matches `datetime.weekday()`. Document choice for future change.

    Returns dict with pwh/pwl + today_inside (bool) + today_close vs PWH/PWL.
    None when <14 daily candles (insufficient for a complete prior week).
    """
    if len(daily_candles) < 14:
        return None
    import datetime as _dt
    today = daily_candles[-1]
    today_dt = _dt.datetime.fromtimestamp(today.timestamp / 1000, tz=_dt.timezone.utc)
    # Days since Monday for today; weekday() Mon=0 ... Sun=6.
    days_since_monday = today_dt.weekday()
    # This week starts at Mon = today - days_since_monday days.
    # Previous week = that minus 7 days, range [-7d, 0d).
    this_week_start = today_dt - _dt.timedelta(days=days_since_monday)
    prev_week_start = this_week_start - _dt.timedelta(days=7)
    prev_week_end = this_week_start - _dt.timedelta(seconds=1)

    prev_week_start_ms = int(prev_week_start.timestamp() * 1000)
    prev_week_end_ms = int(prev_week_end.timestamp() * 1000)

    prev_week_candles = [
        c for c in daily_candles
        if prev_week_start_ms <= c.timestamp <= prev_week_end_ms
    ]
    if not prev_week_candles:
        return None
    pwh = max(c.high for c in prev_week_candles)
    pwl = min(c.low for c in prev_week_candles)
    inside = pwl <= today.close <= pwh
    return {
        "pwh": pwh, "pwl": pwl,
        "inside": inside,
        "today_close": today.close,
        "n_days": len(prev_week_candles),
    }


def _daily_bias_chain(daily_candles: list[Candle], n: int = DAILY_CHAIN_N) -> Optional[dict]:
    """Classify last N daily candles as bull/bear/doji via close vs open.

    Bias of each day uses CLOSE vs OPEN of that daily candle:
      - bull: close > open + |close-open|/open > DAILY_DOJI_TOLERANCE
      - bear: close < open + same threshold
      - doji: |close-open|/open <= DAILY_DOJI_TOLERANCE

    Returns dict with chain list + summary counts. None when <n candles.
    """
    if len(daily_candles) < n:
        return None
    chain = []
    for c in daily_candles[-n:]:
        if c.open <= 0:
            chain.append("doji")
            continue
        delta = abs(c.close - c.open) / c.open
        if delta <= DAILY_DOJI_TOLERANCE:
            chain.append("doji")
        elif c.close > c.open:
            chain.append("bull")
        else:
            chain.append("bear")
    bull = chain.count("bull")
    bear = chain.count("bear")
    doji = chain.count("doji")
    if bull > bear:
        majority = "bull"
        majority_count = bull
    elif bear > bull:
        majority = "bear"
        majority_count = bear
    else:
        majority = "mixed"
        majority_count = max(bull, bear)
    return {
        "chain": chain, "n": n,
        "bull": bull, "bear": bear, "doji": doji,
        "majority": majority, "majority_count": majority_count,
    }


def _today_candle_status(daily_candles: list[Candle]) -> Optional[dict]:
    """Today's daily candle is still forming until UTC midnight.

    Returns {side, forming, close_so_far, open, body_pct}.
      - side: bull|bear|inside derived from close_so_far vs open
      - forming: True if today's candle timestamp matches today's UTC date
    """
    if not daily_candles:
        return None
    import datetime as _dt
    today = daily_candles[-1]
    today_dt = _dt.datetime.fromtimestamp(today.timestamp / 1000, tz=_dt.timezone.utc)
    now_dt = _dt.datetime.fromtimestamp(_now_ms() / 1000, tz=_dt.timezone.utc)
    forming = (today_dt.date() == now_dt.date())

    if today.open <= 0:
        side = "inside"
        body_pct = 0.0
    else:
        delta = (today.close - today.open) / today.open
        body_pct = round(delta * 100, 2)
        if abs(delta) <= DAILY_DOJI_TOLERANCE:
            side = "inside"
        elif delta > 0:
            side = "bull"
        else:
            side = "bear"
    return {
        "side": side, "forming": forming,
        "open": today.open, "close_so_far": today.close,
        "body_pct": body_pct,
    }


# ---------------------------------------------------------------------------
# PR3 v2 — Adaptive TP via Daily ATR(14).
# Scaled (TP1 + TP2) when target distance ≥ multiple * daily_atr; single TP
# otherwise. Crypto-volatility-aware threshold per user pick (grill Q3).
# ---------------------------------------------------------------------------

ATR_PERIOD = 14
ADAPTIVE_TP_ATR_MULTIPLE = 2.0


def _daily_atr(daily_candles: list[Candle], period: int = ATR_PERIOD) -> Optional[float]:
    """ICT-friendly ATR(period) over daily candles. Standard True Range.

    TR = max(high-low, |high - prev_close|, |low - prev_close|).
    ATR = simple average of last `period` TR values.

    Returns ATR in price units, or None when <period+1 candles.
    """
    if len(daily_candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(daily_candles)):
        c = daily_candles[i]
        prev = daily_candles[i - 1]
        tr = max(
            c.high - c.low,
            abs(c.high - prev.close),
            abs(c.low - prev.close),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _adaptive_tp(
    entry: float, sl: float, final_target: float,
    intermediate_candidates: list,
    daily_atr: Optional[float],
    multiple: float = ADAPTIVE_TP_ATR_MULTIPLE,
) -> dict:
    """Decide single vs scaled TP based on target distance / daily ATR.

    - target_distance = |final_target - entry|
    - threshold = multiple * daily_atr
    - If daily_atr unavailable OR target_distance < threshold → single TP.
    - Else → scaled. TP1 = first intermediate liquidity within 1× ATR from
      entry in the direction of the trade. TP2 = final_target. If no
      intermediate qualifies → fall back to single (no synthetic levels).

    intermediate_candidates: list of LiquidityLevel objects between entry
    and final_target, pre-filtered by caller.

    Returns dict with: mode ("single"|"scaled"), tp1, tp2, splits ([50,50]).
    """
    if daily_atr is None or daily_atr <= 0:
        return {"mode": "single", "tp1": final_target, "tp2": None,
                "splits": [100], "atr": daily_atr, "threshold": None,
                "target_distance": abs(final_target - entry)}

    threshold = multiple * daily_atr
    target_distance = abs(final_target - entry)

    if target_distance < threshold:
        return {"mode": "single", "tp1": final_target, "tp2": None,
                "splits": [100], "atr": daily_atr, "threshold": threshold,
                "target_distance": target_distance}

    # Long-distance — try to find intermediate within 1× ATR
    intermediate_range = daily_atr
    is_short = final_target < entry
    valid = []
    for lvl in intermediate_candidates:
        dist_from_entry = abs(lvl.price - entry)
        if dist_from_entry <= 0 or dist_from_entry > intermediate_range:
            continue
        if is_short and lvl.price >= entry:
            continue
        if not is_short and lvl.price <= entry:
            continue
        # Must not be past final_target
        if is_short and lvl.price <= final_target:
            continue
        if not is_short and lvl.price >= final_target:
            continue
        valid.append(lvl)

    if not valid:
        # No clean intermediate → fall back to single TP at final_target
        return {"mode": "single", "tp1": final_target, "tp2": None,
                "splits": [100], "atr": daily_atr, "threshold": threshold,
                "target_distance": target_distance,
                "fallback_reason": "no_intermediate_in_range"}

    # Pick intermediate closest to 1× ATR from entry (mid-range = balanced TP1)
    valid.sort(key=lambda l: abs(abs(l.price - entry) - intermediate_range))
    tp1 = valid[0].price

    return {
        "mode": "scaled", "tp1": tp1, "tp2": final_target,
        "splits": [50, 50], "atr": daily_atr, "threshold": threshold,
        "target_distance": target_distance,
    }


# ---------------------------------------------------------------------------
# PR4 v2 — Structure Context layer.
# Addresses user observation 2026-05-23: brief reads momentum but misses HOW
# LONG HTF structure has held + LTF flips against HTF + single-candle impulse
# events + wick taps into unbroken liquidity. All pure derivation over
# Snapshot data already in memory.
# ---------------------------------------------------------------------------

LTF_FLIP_MAX_CANDLES = 4
IMPULSE_BIG_RATIO = 3.0
IMPULSE_EXTREME_RATIO = 5.0
IMPULSE_BASELINE_N = 30


def _trend_duration(tf_analysis: TFAnalysis, tf_candles: list[Candle]) -> Optional[dict]:
    """How long the current trend on `tf_analysis` has been in place.

    Defined by the last structure break (BOS or CHoCH) — that's when the
    current trend was confirmed. Returns candles_back from the most recent
    candle + wall-clock duration.

    Returns None when no structure break yet or candles missing.
    """
    breaks = tf_analysis.state.structure_breaks
    if not breaks or not tf_candles:
        return None
    latest = breaks[-1]
    latest_ts = latest.timestamp
    last_candle_ts = tf_candles[-1].timestamp
    duration_ms = max(0, last_candle_ts - latest_ts)
    hours = duration_ms / (1000 * 3600)
    days = hours / 24
    # Count candles whose timestamp >= break timestamp
    candles_back = sum(1 for c in tf_candles if c.timestamp >= latest_ts)
    return {
        "trend": tf_analysis.state.trend,
        "since_ts_ms": latest_ts,
        "candles_back": candles_back,
        "hours": round(hours, 1),
        "days": round(days, 1),
        "break_type": latest.break_type,
        "break_direction": latest.direction,
    }


def _ltf_flip_vs_htf(tf_results: dict, htf_tf: str = "4h") -> Optional[dict]:
    """Detect any LTF that flipped AGAINST HTF within LTF_FLIP_MAX_CANDLES.

    For each LTF (15m, 30m, 1h) check:
      - LTF current trend != HTF current trend (opposite directions only count;
        undefined either side is skipped)
      - The most recent structure break on the LTF happened in the last
        LTF_FLIP_MAX_CANDLES candles of that TF

    Returns the LOWEST-TF flip detected (15m > 30m > 1h priority — lowest TF
    is the freshest signal). None when no qualifying flip.
    """
    htf = tf_results.get(htf_tf)
    if htf is None or htf.state.trend not in ("bullish", "bearish"):
        return None
    htf_trend = htf.state.trend
    # Priority order: 15m, 30m, 1h
    for ltf_name in ("15m", "30m", "1h"):
        ltf = tf_results.get(ltf_name)
        if ltf is None:
            continue
        ltf_trend = ltf.state.trend
        if ltf_trend not in ("bullish", "bearish"):
            continue
        if ltf_trend == htf_trend:
            continue
        breaks = ltf.state.structure_breaks
        if not breaks:
            continue
        latest_break = breaks[-1]
        # How many candles ago did the flip happen?
        # latest_break.candle_index is the absolute index in the candle array
        # that confirmed the break. Distance from end = how recent.
        # We don't have the candle array here; use len of structure_breaks
        # since last opposite break as proxy — too brittle. Instead use
        # break_timestamp vs ltf.state.swing_highs/lows tail.
        # Simpler: latest_break.direction must match ltf.state.trend, and
        # we count candles_back from latest BOS confirmation using the
        # break's direction.
        if latest_break.direction != ltf_trend:
            continue
        # We can estimate "candles ago" from how many later breaks exist —
        # since structure_breaks is append-only, the index of latest in the
        # full break list isn't enough. Use the break_timestamp vs current
        # ltf bar count: count breaks whose timestamp >= latest_break.timestamp.
        # Acceptable shortcut: trust candle_index field — distance from len of
        # the analyzed window. Without window length, we treat the break as
        # "fresh" if it is the most recent break AND the trend changed within
        # the window of structure_breaks (use last 2 breaks comparison).
        if len(breaks) >= 2:
            prev_break = breaks[-2]
            if prev_break.direction == htf_trend:
                # The previous LTF break was in HTF direction, and the latest
                # flipped against HTF. That IS the flip event.
                return {
                    "ltf_tf": ltf_name,
                    "ltf_trend": ltf_trend,
                    "htf_trend": htf_trend,
                    "flip_ts_ms": latest_break.timestamp,
                    "flip_type": latest_break.break_type,
                }
        else:
            # Only one break on LTF and it's against HTF — treat as flip.
            return {
                "ltf_tf": ltf_name,
                "ltf_trend": ltf_trend,
                "htf_trend": htf_trend,
                "flip_ts_ms": latest_break.timestamp,
                "flip_type": latest_break.break_type,
            }
    return None


def _last_candle_impulse(candles: list[Candle],
                          baseline_n: int = IMPULSE_BASELINE_N) -> Optional[dict]:
    """Measure body magnitude of last single candle vs baseline.

    Returns dict with magnitude label (big / extreme / normal) + ratio +
    direction. None when insufficient candles.
    """
    if len(candles) < baseline_n + 1:
        return None
    last = candles[-1]
    baseline = candles[-(baseline_n + 1):-1]

    def body_pct(c: Candle) -> float:
        if c.open <= 0:
            return 0.0
        return abs(c.close - c.open) / c.open * 100

    last_body = body_pct(last)
    baseline_avg = sum(body_pct(c) for c in baseline) / len(baseline)
    ratio = last_body / baseline_avg if baseline_avg > 0 else 0.0

    if ratio >= IMPULSE_EXTREME_RATIO:
        magnitude = "extreme"
    elif ratio >= IMPULSE_BIG_RATIO:
        magnitude = "big"
    else:
        magnitude = "normal"

    if last.close > last.open:
        direction = "bull"
    elif last.close < last.open:
        direction = "bear"
    else:
        direction = "doji"

    return {
        "magnitude": magnitude,
        "ratio": round(ratio, 2),
        "body_pct": round(last_body, 2),
        "direction": direction,
    }


def _wick_into_liquidity(candles: list[Candle],
                          liquidity_levels: list) -> Optional[dict]:
    """Detect if last candle's wick tapped an unbroken liquidity level.

    Sweep semantics (matches strategy_service.liquidity.LiquidityAnalyzer):
      - BSL: last.high > level.price AND last.close < level.price → swept BSL
      - SSL: last.low < level.price AND last.close > level.price → swept SSL

    Returns dict with side + tapped level. None when no tap detected.
    Prefers level with highest touch_count (most institutional).
    """
    if not candles or not liquidity_levels:
        return None
    last = candles[-1]
    candidates = []
    for lvl in liquidity_levels:
        if lvl.swept:
            continue
        if lvl.level_type == "bsl":
            if last.high > lvl.price and last.close < lvl.price:
                candidates.append(("bsl", lvl))
        elif lvl.level_type == "ssl":
            if last.low < lvl.price and last.close > lvl.price:
                candidates.append(("ssl", lvl))
    if not candidates:
        return None
    # Pick the level with highest touch_count
    candidates.sort(key=lambda t: t[1].touch_count, reverse=True)
    side, lvl = candidates[0]
    return {
        "side": side,
        "level_price": lvl.price,
        "touch_count": lvl.touch_count,
    }


# ---------------------------------------------------------------------------
# Brief usage tracking — topdown_brief_renders table (Phase 3 falsification)
# ---------------------------------------------------------------------------


def ensure_topdown_renders_table() -> None:
    """Create the topdown_brief_renders table if missing.

    Phase 3 falsification (docs/plans/topdown-ict-enhancements-2026-05-23.md)
    joins this to bybit_trade_annotations on (pair, opened_at within 30 min of
    rendered_at) to bucket trades as brief-informed vs control.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS topdown_brief_renders (
        id BIGSERIAL PRIMARY KEY,
        pair VARCHAR(20) NOT NULL,
        rendered_at TIMESTAMPTZ DEFAULT NOW(),
        brief_mode VARCHAR(20)
    );
    CREATE INDEX IF NOT EXISTS idx_topdown_renders_pair_time
        ON topdown_brief_renders(pair, rendered_at DESC);
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
    finally:
        conn.close()


def log_brief_render(pair: str, brief_mode: str) -> None:
    """Insert a row marking that /topdown produced a brief.

    Failure must NOT block the user-facing reply — caller wraps in try/except.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO topdown_brief_renders (pair, brief_mode) VALUES (%s, %s)",
                (pair, brief_mode),
            )
            conn.commit()
    finally:
        conn.close()


def _build_snapshot(cur, conn, pair: str) -> Optional[Snapshot]:
    # Ensure 30m candles available before loading
    inserted = _ensure_30m_backfill(cur, conn, pair)
    if inserted:
        print(f"[{pair}] backfilled {inserted} 30m candles")

    candles_by_tf: dict[str, list[Candle]] = {}
    for tf in CASCADE_TFS + ["5m", "1d"]:
        candles_by_tf[tf] = _load_candles(
            cur, pair, tf, CANDLE_LIMIT_PER_TF.get(tf, 200)
        )
        if not candles_by_tf[tf]:
            print(f"[{pair}] WARN: no candles for {tf}")
    if not candles_by_tf["5m"]:
        return None

    current_price = candles_by_tf["5m"][-1].close
    current_time_ms = candles_by_tf["5m"][-1].timestamp

    tf_results: dict[str, TFAnalysis] = {}
    for tf in CASCADE_TFS:
        if candles_by_tf[tf]:
            tf_results[tf] = _analyze_tf(
                candles_by_tf[tf], pair, tf, current_time_ms,
            )

    vp_analyzer = VolumeProfileAnalyzer()
    vp = vp_analyzer.update(pair, candles_by_tf["4h"]) if candles_by_tf["4h"] else None

    side, confidence = _reconcile(tf_results)
    inv_level, inv_reason = _pick_invalidation(side, current_price, tf_results)

    return Snapshot(
        pair=pair,
        current_price=current_price,
        current_time_ms=current_time_ms,
        tf_results=tf_results,
        vp=vp,
        reconciled_side=side,
        confidence=confidence,
        invalidation_level=inv_level,
        invalidation_reason=inv_reason,
        raw_candles=candles_by_tf,
    )


def build_edge_signal(pair: str) -> Optional[dict]:
    """Public edge-triplet helper consumed by the signal scanner.

    Builds a Snapshot for `pair` and returns the /topdown trade triplet as a
    flat signal dict when the play is valid, else None. Behavior-preserving:
    reuses `_build_snapshot` + `_trade_triplet` verbatim and alters no /topdown
    brief path. The scanner consumes this; /topdown output stays byte-identical.

    Returns None when no snapshot can be built, the reconciled side is
    undefined, or the triplet is not `valid` (sweep too far, SL wrong side,
    no target). Callers apply their own tighter gates (e.g. sweep ≤0.5%).
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            snap = _build_snapshot(cur, conn, pair)
    finally:
        conn.close()
    if snap is None:
        return None
    triplet = _trade_triplet(snap)
    if not triplet or not triplet.get("valid"):
        return None
    return {
        "pair": pair,
        "side": snap.reconciled_side,
        "entry": triplet["entry"],
        "sl": triplet["sl"],
        "tp": triplet["tp"],
        "rr": triplet["rr"],
        "sweep_distance_pct": triplet["sweep_distance_pct"],
        "risk_pct": triplet["risk_pct"],
        "bias_confidence": snap.confidence,
        "current_price": snap.current_price,
    }


def _format_zone_distance(price: float, current: float) -> str:
    pct = 100.0 * (price - current) / current
    return f"{pct:+.2f}%"


def _filter_near(items, price_attr, current: float, max_n: int = 3):
    """Return up to max_n items whose price_attr is within NEAR_PRICE_PCT of current."""
    filtered = []
    for it in items:
        p = price_attr(it)
        if p == 0:
            continue
        pct = abs(p - current) / current
        if pct <= NEAR_PRICE_PCT:
            filtered.append((pct, it))
    filtered.sort(key=lambda x: x[0])
    return [it for _, it in filtered[:max_n]]


def _interpret(snap: Snapshot) -> list[str]:
    """Generate 3-5 line plain-language interpretation from the snapshot."""
    lines: list[str] = []

    trends = {tf: snap.tf_results[tf].state.trend
              for tf in CASCADE_TFS if tf in snap.tf_results}
    htf_trend = trends.get("4h", "undefined")
    bull_score = sum(TF_WEIGHTS.get(tf, 1) for tf, t in trends.items() if t == "bullish")
    bear_score = sum(TF_WEIGHTS.get(tf, 1) for tf, t in trends.items() if t == "bearish")
    side = snap.reconciled_side
    conf = snap.confidence

    # Line 1: reconciled side narrative (HTF-weighted score wording)
    if conf == "high" and side == "long":
        lines.append(f"Plain read: HTF-anchored LONG (weighted {bull_score}/5). Buyers in control with 4H alignment.")
    elif conf == "high" and side == "short":
        lines.append(f"Plain read: HTF-anchored SHORT (weighted {bear_score}/5). Sellers in control with 4H alignment.")
    elif conf == "medium" and side == "long":
        lines.append(f"Plain read: LONG bias (weighted {bull_score}/5, 4H={htf_trend}). Moderate conviction.")
    elif conf == "medium" and side == "short":
        lines.append(f"Plain read: SHORT bias (weighted {bear_score}/5, 4H={htf_trend}). Moderate conviction.")
    elif conf == "low" and side == "long":
        lines.append(f"Plain read: LONG lean but 4H disagrees ({bull_score}/5 weighted). Low conviction — wait or size small.")
    elif conf == "low" and side == "short":
        lines.append(f"Plain read: SHORT lean but 4H disagrees ({bear_score}/5 weighted). Low conviction — wait or size small.")
    else:
        lines.append("Plain read: No clear direction — TFs split or undefined. Wait or size small.")

    # Line 2: HTF vs LTF disagreement flag (key for swing trader)
    htf_trend = trends.get("4h", "undefined")
    ltf_trends = [trends.get("30m", "undefined"), trends.get("15m", "undefined")]
    if htf_trend in ("bullish", "bearish") and all(t != htf_trend and t != "undefined" for t in ltf_trends):
        lines.append(f"  HTF/LTF split: 4H={htf_trend} but lower TFs disagree — likely pullback or trend exhaustion.")

    # Line 3: nearest unbroken liquidity (any TF) within 2%
    all_liq = []
    for tf, tfa in snap.tf_results.items():
        for lvl in tfa.liquidity:
            if not lvl.swept:
                pct = abs(lvl.price - snap.current_price) / snap.current_price
                if pct <= 0.02:
                    all_liq.append((pct, lvl, tf))
    all_liq.sort(key=lambda x: x[0])
    if all_liq:
        _, lvl, tf = all_liq[0]
        dist = (lvl.price - snap.current_price) / snap.current_price * 100
        side_word = "above (buy-stops, magnet up)" if lvl.level_type == "bsl" else "below (sell-stops, magnet down)"
        lines.append(f"  Watch level: {lvl.price:.6g} ({dist:+.2f}%) {side_word}, {lvl.touch_count} touches on {tf} — UNBROKEN.")

    # Line 4: invalidation distance
    if snap.invalidation_level is not None:
        dist = abs(snap.invalidation_level - snap.current_price) / snap.current_price * 100
        if dist < 0.5:
            tight_word = "TIGHT — barely room to breathe (risk fast stop)"
        elif dist < 1.5:
            tight_word = "moderate room"
        else:
            tight_word = "comfortable room"
        lines.append(f"  Invalidation: {snap.invalidation_level:.6g} ({dist:.2f}% away) — {tight_word}.")

    return lines


def _min_target_distance(sweep_level: Optional[float],
                         invalidation_level: Optional[float]) -> Optional[float]:
    """1.5R floor: target must be at least 1.5x the entry-invalidation distance away.

    Fix for SOL incident 2026-05-22 — previous code picked nearest unbroken
    liquidity as target regardless of distance, producing targets like 84.123
    when sweep entry was 84.12 (noise, not profit).
    Returns None if either input is None — caller skips filtering.
    """
    if sweep_level is None or invalidation_level is None:
        return None
    return abs(sweep_level - invalidation_level) * TARGET_MIN_R_MULTIPLE


def _pick_valid_target(
    candidates: list, side: str, sweep_level: float, min_distance: Optional[float],
):
    """Pick first liquidity that satisfies min target distance.

    candidates are LiquidityLevel objects already filtered to one side of price.
    For short: pick liquidity below sweep, ordered ascending by price (furthest
    valid below the floor). For long: pick liquidity above sweep, ordered
    descending. Returns None if no candidate qualifies.
    """
    if not candidates or min_distance is None:
        # No floor available — fall back to nearest (original behavior)
        return candidates[0] if candidates else None

    for lvl in candidates:
        if abs(lvl.price - sweep_level) >= min_distance:
            return lvl
    return None


def _play_idea(snap: Snapshot) -> list[str]:
    """Rule-based actionable plan in plain words. NOT a signal — a scenario."""
    side = snap.reconciled_side
    conf = snap.confidence
    price = snap.current_price

    # Collect ALL unbroken liquidity above and below, sorted by distance
    above_levels = []
    below_levels = []
    for tf, tfa in snap.tf_results.items():
        for lvl in tfa.liquidity:
            if lvl.swept:
                continue
            if lvl.price > price:
                above_levels.append(lvl)
            elif lvl.price < price:
                below_levels.append(lvl)
    above_levels.sort(key=lambda l: l.price)         # nearest first
    below_levels.sort(key=lambda l: l.price, reverse=True)  # nearest first

    liq_above = above_levels[0] if above_levels else None
    liq_below = below_levels[0] if below_levels else None

    lines = ["PLAY IDEA:"]
    if conf == "low" or side == "undefined":
        lines.append("  Wait. TFs split or 4H disagrees. No clean setup.")
        return lines

    if side == "short":
        # Look for sweep above to short the rejection
        sweep_level = liq_above.price if liq_above else None
        if liq_above and (liq_above.price - price) / price < 0.015:
            lines.append(
                f"  Wait for sweep above {liq_above.price:.6g} "
                f"(+{(liq_above.price - price) / price * 100:.2f}%) "
                f"then short on rejection back below."
            )
        else:
            lines.append("  Short at market or pullback into supply zone. Bias is down.")
        if snap.invalidation_level:
            lines.append(f"  Invalidate: 4H close above {snap.invalidation_level:.6g}.")
        # Bug fix: enforce 1.5R floor between sweep and target. Iterate
        # candidates ranked by distance, pick first that satisfies floor.
        min_dist = _min_target_distance(sweep_level, snap.invalidation_level)
        target = _pick_valid_target(below_levels, "short", sweep_level or price, min_dist)
        if target:
            lines.append(
                f"  Target / partial: {target.price:.6g} "
                f"({(target.price - price) / price * 100:+.2f}%, sell-stops below)."
            )
        elif below_levels:
            nearest = below_levels[0]
            lines.append(
                f"  No target ≥{TARGET_MIN_R_MULTIPLE}R from sweep "
                f"(skipped {nearest.price:.6g} — too tight). Find manually."
            )

    elif side == "long":
        sweep_level = liq_below.price if liq_below else None
        if liq_below and (price - liq_below.price) / price < 0.015:
            lines.append(
                f"  Wait for sweep below {liq_below.price:.6g} "
                f"({(liq_below.price - price) / price * 100:+.2f}%) "
                f"then long on reclaim."
            )
        else:
            lines.append("  Long at market or pullback into demand zone. Bias is up.")
        if snap.invalidation_level:
            lines.append(f"  Invalidate: 4H close below {snap.invalidation_level:.6g}.")
        min_dist = _min_target_distance(sweep_level, snap.invalidation_level)
        # For long target above: sort ascending (furthest valid)
        above_for_target = sorted(above_levels, key=lambda l: l.price)
        target = _pick_valid_target(above_for_target, "long", sweep_level or price, min_dist)
        if target:
            lines.append(
                f"  Target / partial: {target.price:.6g} "
                f"({(target.price - price) / price * 100:+.2f}%, buy-stops above)."
            )
        elif above_levels:
            nearest = above_levels[0]
            lines.append(
                f"  No target ≥{TARGET_MIN_R_MULTIPLE}R from sweep "
                f"(skipped {nearest.price:.6g} — too tight). Find manually."
            )

    return lines


def _render_short(snap: Snapshot) -> str:
    """Compact mobile-friendly output: recommendation + key levels + play idea."""
    lines = []
    ts_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(snap.current_time_ms / 1000))
    lines.append(f"{snap.pair} — {ts_str}")
    lines.append(f"Price: {snap.current_price:.6g}")
    lines.append("")

    # Recommendation block
    lines.append(">>> RECOMMENDATION <<<")
    for ln in _interpret(snap):
        lines.append(ln)
    lines.append("")

    # Play idea
    for ln in _play_idea(snap):
        lines.append(ln)
    lines.append("")
    lines.append(f"For full technical detail: /topdown {snap.pair.split('/')[0].lower()} full")
    return "\n".join(lines)


def _render(snap: Snapshot) -> str:
    lines = []
    ts_str = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(snap.current_time_ms / 1000))
    lines.append("=" * 72)
    lines.append(f"{snap.pair} — Top-Down Brief  ({ts_str})")
    lines.append(f"Current price: {snap.current_price:.6g}")
    lines.append("")

    # Narrative interpretation FIRST (the short read)
    for ln in _interpret(snap):
        lines.append(ln)
    lines.append("")
    lines.append("--- Technical detail ---")
    lines.append("")

    # Per-TF summary
    for tf in CASCADE_TFS:
        tfa = snap.tf_results.get(tf)
        if tfa is None:
            lines.append(f"[{tf.upper()}] NO DATA")
            continue
        trend = tfa.state.trend
        latest = tfa.state.latest_break
        brk_str = (
            f"last={latest.break_type.upper()} {latest.direction} @ "
            f"{latest.broken_level:.6g}"
            if latest else "no break yet"
        )
        n_obs_near = _filter_near(tfa.obs, lambda o: o.entry_price, snap.current_price, 3)
        n_fvgs_near = _filter_near(
            tfa.fvgs, lambda f: (f.high + f.low) / 2, snap.current_price, 2
        )
        unbroken_liq = [lvl for lvl in tfa.liquidity if not lvl.swept]
        liq_near = _filter_near(unbroken_liq, lambda l: l.price, snap.current_price, 3)
        lines.append(f"[{tf.upper()}] trend={trend} | {brk_str}")
        if n_obs_near:
            for ob in n_obs_near:
                lines.append(
                    f"  OB {ob.direction} @ {ob.entry_price:.6g} "
                    f"(zone {ob.body_low:.6g}-{ob.body_high:.6g}, "
                    f"score={ob.impulse_score:.2f}, "
                    f"{_format_zone_distance(ob.entry_price, snap.current_price)})"
                )
        if n_fvgs_near:
            for fvg in n_fvgs_near:
                mid = (fvg.high + fvg.low) / 2
                lines.append(
                    f"  FVG {fvg.direction} {fvg.low:.6g}-{fvg.high:.6g} "
                    f"(filled {fvg.filled_pct * 100:.0f}%, "
                    f"{_format_zone_distance(mid, snap.current_price)})"
                )
        if liq_near:
            for lvl in liq_near:
                tag = "BSL (highs)" if lvl.level_type == "bsl" else "SSL (lows)"
                lines.append(
                    f"  Liquidity {tag} @ {lvl.price:.6g} "
                    f"(touches={lvl.touch_count}, UNBROKEN, "
                    f"{_format_zone_distance(lvl.price, snap.current_price)})"
                )
        lines.append("")

    # Volume profile
    if snap.vp:
        lines.append(
            f"[4H VP] POC={snap.vp.poc_price:.6g} "
            f"VAH={snap.vp.vah:.6g} VAL={snap.vp.val:.6g}"
        )
        lines.append(
            f"  POC dist {_format_zone_distance(snap.vp.poc_price, snap.current_price)} | "
            f"VAH dist {_format_zone_distance(snap.vp.vah, snap.current_price)} | "
            f"VAL dist {_format_zone_distance(snap.vp.val, snap.current_price)}"
        )
        lines.append("")
    else:
        lines.append("[4H VP] insufficient data")
        lines.append("")

    # Reconciliation
    lines.append("RECONCILED")
    lines.append(f"  Side: {snap.reconciled_side.upper()}")
    lines.append(f"  Confidence: {snap.confidence}")
    if snap.invalidation_level is not None:
        lines.append(
            f"  Invalidation: {snap.invalidation_level:.6g} "
            f"({_format_zone_distance(snap.invalidation_level, snap.current_price)}) "
            f"— {snap.invalidation_reason}"
        )
    else:
        lines.append(f"  Invalidation: N/A ({snap.invalidation_reason})")
    lines.append("")
    lines.append("Confidence ranges: high=all TFs agree, medium=majority, low=split/undefined.")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram Markdown renderer — mobile-first, pure SMC/ICT vocabulary.
# Sections: header → BIAS → ICT STRENGTH → KEY ZONES → MAGNETS → PLAY →
# INVALIDATION → freshness footer. Target ≤30 rendered lines.
# ---------------------------------------------------------------------------


TELEGRAM_REQUIRED_SECTIONS = (
    "*BIAS:*", "*ICT STRENGTH:*", "*KEY ZONES:*",
    "*MAGNETS BELOW:*", "*PLAY:*", "*INVALIDATION:*",
)
# DAILY CONTEXT is data-dependent (requires ≥2 daily candles); checked via
# _has_optional_daily_context_section instead.


def _render_telegram_markdown(snap: Snapshot) -> str:
    """Mobile Telegram-Markdown brief — pure ICT reads, ≤30 lines.

    Uses `*bold*` headers, `_italic_` matiz, `` `code` `` for prices, emoji
    flags for 3-second scan. Compatible with Telegram `parse_mode='Markdown'`.
    """
    lines: list[str] = []
    pair = snap.pair
    price = snap.current_price
    ts_str = time.strftime("%Y-%m-%d %H:%M UTC",
                           time.gmtime(snap.current_time_ms / 1000))
    lag_sec = max(0, int(_now_ms() / 1000 - snap.current_time_ms / 1000))
    lag_min = lag_sec // 60
    lag_flag = (
        "✅" if lag_min <= FRESHNESS_OK_MIN
        else ("⚠️" if lag_min <= FRESHNESS_WARN_MIN else "⚠️ STALE")
    )

    # Header
    lines.append(f"*{pair}* — {ts_str} (lag {lag_min}m {lag_flag})")
    lines.append(f"Price: `{price:.6g}`")
    lines.append("")

    # BIAS — compute PD zone first to detect conflict for confidence suffix
    side = snap.reconciled_side
    conf = snap.confidence
    side_emoji = {"long": "🟢", "short": "🔴",
                  "undefined": "⚪"}.get(side, "⚪")
    trends = {tf: snap.tf_results[tf].state.trend
              for tf in CASCADE_TFS if tf in snap.tf_results}
    bull_score = sum(TF_WEIGHTS.get(tf, 1) for tf, t in trends.items()
                     if t == "bullish")
    bear_score = sum(TF_WEIGHTS.get(tf, 1) for tf, t in trends.items()
                     if t == "bearish")
    total_weight = sum(TF_WEIGHTS.get(tf, 1) for tf in trends)
    score = bull_score if side == "long" else (
        bear_score if side == "short" else max(bull_score, bear_score)
    )

    # Precompute PD info for conflict detection (needed before BIAS render)
    htf = snap.tf_results.get("4h")
    pd_info = None
    if htf is not None:
        candles_4h = snap.raw_candles.get("4h", []) if hasattr(snap, "raw_candles") else []
        if candles_4h:
            pd_info = _pd_array_position(
                htf_candles=candles_4h, htf_state=htf.state,
                pair=pair, current_price=price, current_time_ms=snap.current_time_ms,
            )
    pd_zone = pd_info["zone"] if pd_info else None
    has_pd_conflict = _pd_bias_conflict(side, pd_zone)
    conf_suffix = " — _PD conflict_" if has_pd_conflict else ""

    lines.append(
        f"*BIAS:* {side_emoji} {side.upper()} — _{conf}_{conf_suffix} "
        f"({score}/{total_weight})"
    )

    # DAILY CONTEXT — PR2 v2 (3 lines max: today + chain + weekly)
    daily_candles = snap.raw_candles.get("1d", []) if hasattr(snap, "raw_candles") else []
    if daily_candles:
        lines.append("")
        lines.append("*DAILY CONTEXT:*")

        # Line 1: today's forming candle + PDH/PDL status
        today_status = _today_candle_status(daily_candles)
        pdh_pdl = _compute_pdh_pdl(daily_candles)
        if today_status and pdh_pdl:
            t_emoji = {"bull": "🟢", "bear": "🔴",
                       "inside": "⚪"}.get(today_status["side"], "⚪")
            forming_tag = "forming" if today_status["forming"] else "closed"
            pdh_tag = pdh_pdl["pdh_status"]
            pdl_tag = pdh_pdl["pdl_status"]
            lines.append(
                f"Today: {t_emoji} {today_status['side']} {forming_tag} "
                f"({today_status['body_pct']:+.2f}%) — "
                f"PDH `{pdh_pdl['pdh']:.6g}` _{pdh_tag}_, "
                f"PDL `{pdh_pdl['pdl']:.6g}` _{pdl_tag}_"
            )

        # Line 2: 5-day bias chain
        chain = _daily_bias_chain(daily_candles)
        if chain:
            chain_emoji = {"bull": "🟢", "bear": "🔴",
                           "doji": "⚪"}.get
            chain_str = " ".join(chain_emoji(c) or "⚪" for c in chain["chain"])
            majority_word = chain["majority"]
            lines.append(
                f"Chain ({chain['n']}d): {chain_str}  "
                f"({chain['bull']}b / {chain['bear']}s / {chain['doji']}d "
                f"→ {majority_word})"
            )

        # Line 3: weekly inside/broken
        pwh_pwl = _compute_pwh_pwl(daily_candles)
        if pwh_pwl:
            inside_tag = "inside" if pwh_pwl["inside"] else "broken"
            w_emoji = "⚪" if pwh_pwl["inside"] else "🔴"
            lines.append(
                f"Weekly: {w_emoji} _{inside_tag}_ "
                f"(PWH `{pwh_pwl['pwh']:.6g}`, PWL `{pwh_pwl['pwl']:.6g}`)"
            )

    # STRUCTURE CONTEXT — PR4 v2 (max 4 lines, conditional per signal)
    structure_lines: list[str] = []
    htf_4h = snap.tf_results.get("4h")
    candles_4h_raw = snap.raw_candles.get("4h", []) if hasattr(snap, "raw_candles") else []
    candles_1h_raw = snap.raw_candles.get("1h", []) if hasattr(snap, "raw_candles") else []

    # Line 1: HTF trend duration
    if htf_4h is not None and candles_4h_raw:
        dur = _trend_duration(htf_4h, candles_4h_raw)
        if dur is not None and dur["trend"] in ("bullish", "bearish"):
            t_emoji = "🔴" if dur["trend"] == "bearish" else "🟢"
            ts_str = time.strftime("%Y-%m-%d %H:%M UTC",
                                   time.gmtime(dur["since_ts_ms"] / 1000))
            structure_lines.append(
                f"4H {dur['trend']} since {ts_str} "
                f"({dur['days']}d {int(dur['hours'] % 24)}h, "
                f"{dur['candles_back']} candles) {t_emoji}"
            )

    # Line 2: LTF flip vs HTF (countertrend warning)
    flip = _ltf_flip_vs_htf(snap.tf_results)
    if flip is not None:
        structure_lines.append(
            f"⚠️ {flip['ltf_tf']} flipped {flip['ltf_trend']} vs 4H "
            f"{flip['htf_trend']} — likely pullback, NOT confirmed reversal"
        )

    # Line 3: Last 1H candle impulse event
    if candles_1h_raw:
        imp = _last_candle_impulse(candles_1h_raw)
        if imp is not None and imp["magnitude"] in ("big", "extreme"):
            i_emoji = "🟢" if imp["direction"] == "bull" else "🔴"
            mag_word = "EXTREME" if imp["magnitude"] == "extreme" else "big"
            structure_lines.append(
                f"Last 1H: {i_emoji} {mag_word} {imp['direction']} "
                f"impulse ({imp['body_pct']:+.2f}%, x{imp['ratio']} baseline)"
            )

    # Line 4: Wick into unbroken liquidity on last 1H
    if candles_1h_raw and htf_4h is not None:
        all_liq = []
        for tfa in snap.tf_results.values():
            all_liq.extend(tfa.liquidity)
        tap = _wick_into_liquidity(candles_1h_raw, all_liq)
        if tap is not None:
            side_label = "BSL" if tap["side"] == "bsl" else "SSL"
            structure_lines.append(
                f"Last 1H wick tapped {side_label} `{tap['level_price']:.6g}` "
                f"({tap['touch_count']} touches) — possible liquidity sweep"
            )

    if structure_lines:
        lines.append("")
        lines.append("*STRUCTURE CONTEXT:*")
        lines.extend(structure_lines)

    # ICT STRENGTH section — pure SMC reads
    lines.append("")
    lines.append("*ICT STRENGTH:*")

    # 4H displacement
    htf = snap.tf_results.get("4h")
    if htf is not None:
        # Pull raw candles for the 4H TF from the analyzer state — use the
        # swing-derived structure_breaks for richer signals, but for body
        # measurements we need raw candles. The TFAnalysis doesn't store
        # candles; helper accepts candles directly. We re-load them inline
        # to keep the helper pure.
        disp = _build_displacement_for_tf(snap, "4h")
        if disp and disp["strength"] != "unknown":
            disp_emoji = {"strong": "🟢", "moderate": "🟡",
                          "weak": "🔴"}.get(disp["strength"], "⚪")
            lines.append(
                f"• Displacement 4H: {disp_emoji} _{disp['strength']}_ "
                f"({disp['direction']}, body x{disp['body_ratio']})"
            )

    # PD Array position (4H range) — pd_info computed above for conflict check
    if pd_info:
        zone_emoji = {"premium": "🔴", "discount": "🟢",
                      "equilibrium": "⚪"}.get(pd_info["zone"], "⚪")
        zone_hint = ""
        if side == "short" and pd_info["zone"] == "premium":
            zone_hint = " — favorable shorts"
        elif side == "long" and pd_info["zone"] == "discount":
            zone_hint = " — favorable longs"
        lines.append(
            f"• PD Array 4H: {zone_emoji} {pd_info['position_pct']}% "
            f"_{pd_info['zone']}_{zone_hint}"
        )
        if has_pd_conflict:
            lines.append(
                "  ⚠️ *PD-BIAS CONFLICT* — counter-PD trade, downgrade conviction"
            )

    # IDM on last BOS + session quality
    if htf is not None:
        idm = _inducement_check(htf)
        latest = htf.state.latest_break
        session_q = _bos_session_quality(latest.timestamp if latest else None)
        session_suffix = ""
        if session_q:
            sess_emoji = "🟢" if session_q["quality"] == "high" else "🟡"
            session_suffix = (
                f"  {sess_emoji} _{session_q['session']} session "
                f"({session_q['quality']} quality)_"
            )
        if idm["has_idm"]:
            lines.append(
                f"• Last BOS: 🟢 _IDM confirmed_ "
                f"(swept `{idm['idm_level']:.6g}`)"
            )
            if session_suffix:
                lines.append(session_suffix)
        elif latest is not None:
            lines.append("• Last BOS: ⚪ _spontaneous (no IDM)_")
            if session_suffix:
                lines.append(session_suffix)

    # Killzone
    kz = _killzone_now(snap.current_time_ms)
    if kz["active"]:
        lines.append(f"• Killzone: 🟢 _{kz['name']} active_")
    else:
        h = kz["minutes_to_next"] // 60
        m = kz["minutes_to_next"] % 60
        eta = f"{h}h{m}m" if h else f"{m}m"
        lines.append(
            f"• Killzone: ⚪ _dead zone_ ({kz['next_name']} in {eta})"
        )

    # KEY ZONES (aligned to bias)
    lines.append("")
    lines.append("*KEY ZONES:*")
    aligned_dir = "bearish" if side == "short" else (
        "bullish" if side == "long" else None
    )
    zone_count = 0
    for tf in ("4h", "1h"):
        tfa = snap.tf_results.get(tf)
        if tfa is None or aligned_dir is None:
            continue
        nearest_obs = _filter_near(
            [ob for ob in tfa.obs if ob.direction == aligned_dir],
            lambda o: o.entry_price, price, 2,
        )
        for ob in nearest_obs:
            status = "MITIGATED" if ob.mitigated else "PRISTINE"
            emoji = "🔴" if aligned_dir == "bearish" else "🟢"
            lines.append(
                f"{emoji} {tf.upper()} OB `{ob.entry_price:.6g}` {status} "
                f"({_format_zone_distance(ob.entry_price, price)})"
            )
            zone_count += 1
            if zone_count >= 3:
                break
        if zone_count >= 3:
            break
    if zone_count == 0:
        lines.append("_(no aligned zones within 3% — wait or pull HTF cascade)_")

    # MAGNETS BELOW / ABOVE (unfilled FVGs + unbroken SSL/BSL)
    lines.append("")
    magnet_label = "MAGNETS BELOW" if side == "short" else "MAGNETS ABOVE"
    lines.append(f"*{magnet_label}:*")
    magnets: list[str] = []
    if side == "short":
        below_ssl = []
        for tfa in snap.tf_results.values():
            for lvl in tfa.liquidity:
                if not lvl.swept and lvl.level_type == "ssl" and lvl.price < price:
                    below_ssl.append(lvl)
        below_ssl.sort(key=lambda l: l.price, reverse=True)
        for lvl in below_ssl[:2]:
            magnets.append(
                f"🟢 SSL `{lvl.price:.6g}` × {lvl.touch_count} "
                f"({_format_zone_distance(lvl.price, price)})"
            )
    else:
        above_bsl = []
        for tfa in snap.tf_results.values():
            for lvl in tfa.liquidity:
                if not lvl.swept and lvl.level_type == "bsl" and lvl.price > price:
                    above_bsl.append(lvl)
        above_bsl.sort(key=lambda l: l.price)
        for lvl in above_bsl[:2]:
            magnets.append(
                f"🟢 BSL `{lvl.price:.6g}` × {lvl.touch_count} "
                f"({_format_zone_distance(lvl.price, price)})"
            )
    if not magnets:
        magnets.append("_(no unbroken liquidity beyond price on aligned side)_")
    lines.extend(magnets)

    # PLAY — explicit Entry/SL/TP/R:R triplet (PR1 v2 enhancement) when actionable
    lines.append("")
    lines.append("*PLAY:*")
    triplet = _trade_triplet(snap)
    if triplet is None:
        # No bias or no invalidation — fall back to narrative
        for ln in _play_idea(snap)[1:]:
            cleaned = ln.lstrip()
            if cleaned:
                lines.append(cleaned)
    elif triplet.get("valid") is False:
        if triplet.get("reason") == "sweep_too_far":
            dist = triplet.get("sweep_distance_pct", 0)
            lines.append(
                f"⚠️ Sweep too far ({dist:.1f}% away) — spectator zone. "
                f"Wait for LTF setup or skip pair."
            )
        elif triplet.get("reason") == "no_valid_target":
            entry_v = triplet.get("entry")
            sl_v = triplet.get("sl")
            lines.append(
                f"Entry: `{entry_v:.6g}`  SL: `{sl_v:.6g}`  "
                f"TP: _none ≥1.5R, find manually_"
            )
    else:
        e = triplet["entry"]
        sl = triplet["sl"]
        lines.append(f"Entry: `{e:.6g}`  (sweep, {triplet['sweep_distance_pct']:+.2f}%)")
        lines.append(f"SL:    `{sl:.6g}`  ({triplet['risk_pct']:.2f}% risk)")
        # PR3: scaled vs single TP via daily ATR multiple
        if triplet.get("tp_mode") == "scaled" and triplet.get("tp2") is not None:
            tp1 = triplet["tp1"]
            tp2 = triplet["tp2"]
            lines.append(
                f"TP1:   `{tp1:.6g}`  (50%, R:R {triplet.get('rr_tp1', 0):.2f})"
            )
            lines.append(
                f"TP2:   `{tp2:.6g}`  (50%, R:R {triplet.get('rr_tp2', 0):.2f})"
            )
            lines.append(
                f"_scaled — target ≥2× daily ATR (`{triplet.get('atr', 0):.4g}`)_"
            )
        else:
            tp = triplet["tp"]
            lines.append(f"TP:    `{tp:.6g}`  ({triplet['reward_pct']:.2f}% reward)")
            lines.append(f"R:R:   `{triplet['rr']:.2f}`")

    # INVALIDATION
    lines.append("")
    if snap.invalidation_level is not None:
        lines.append(
            f"*INVALIDATION:* 4H close "
            f"{'above' if side == 'short' else 'below'} "
            f"`{snap.invalidation_level:.6g}`"
        )
    else:
        lines.append(f"*INVALIDATION:* _N/A ({snap.invalidation_reason})_")

    return "\n".join(lines)


def _build_displacement_for_tf(snap: Snapshot, tf: str) -> Optional[dict]:
    """Compute displacement read for a TF using raw candles attached to snap.

    Snapshot does not currently carry raw candles. We retrieve them lazily
    via a DB read keyed by the snapshot's pair + lookback window. Caller
    discards None if data is unavailable.
    """
    if not hasattr(snap, "raw_candles") or tf not in snap.raw_candles:
        return None
    return _displacement_read(snap.raw_candles[tf])


def _has_required_sections(rendered: str) -> bool:
    required_markers = [
        "Top-Down Brief",
        "Current price:",
        "Plain read:",
        "[4H]",
        "[1H]",
        "[30M]",
        "[15M]",
        "[4H VP]",
        "RECONCILED",
        "Side:",
        "Confidence:",
        "Invalidation:",
    ]
    return all(m in rendered for m in required_markers)


def _has_required_telegram_sections(rendered: str) -> bool:
    """Golden-file check for Telegram-mode brief."""
    return all(m in rendered for m in TELEGRAM_REQUIRED_SECTIONS)


def _narrative_line_count(rendered: str) -> int:
    """Count lines between 'Plain read:' and '--- Technical detail ---'."""
    lines = rendered.split("\n")
    started = False
    count = 0
    for ln in lines:
        if "Plain read:" in ln:
            started = True
        if started:
            if "--- Technical detail ---" in ln:
                break
            if ln.strip():
                count += 1
    return count


_PAIR_ALIASES = {
    "btc": "BTC/USDT", "eth": "ETH/USDT", "xrp": "XRP/USDT", "sol": "SOL/USDT",
}


def normalize_pair(raw: str) -> Optional[str]:
    """Accept 'btc', 'BTC', 'BTC/USDT' → canonical 'BTC/USDT'.

    Returns None if the resulting pair is not in PAIRS (supported subset).
    """
    if not raw:
        return None
    key = raw.strip().upper()
    if "/" not in key:
        canonical = _PAIR_ALIASES.get(key.lower())
        if canonical and canonical in PAIRS:
            return canonical
        candidate = f"{key}/USDT"
        return candidate if candidate in PAIRS else None
    return key if key in PAIRS else None


def build_brief_text(pair: str, mode: str = "telegram") -> Optional[str]:
    """Public entry point. Build + render the brief text for a single pair.

    mode:
      'telegram' (default): Telegram-Markdown mobile-first brief, pure SMC/ICT.
      'short': legacy compact console output (recommendation + play idea).
      'full': full multi-section technical detail (console).

    Returns None if pair has insufficient data.
    """
    conn = _connect()
    cur = conn.cursor()
    try:
        snap = _build_snapshot(cur, conn, pair)
        if snap is None:
            return None
        if mode == "full":
            return _render(snap)
        if mode == "short":
            return _render_short(snap)
        return _render_telegram_markdown(snap)
    finally:
        cur.close()
        conn.close()


def build_brief_and_state(pair: str) -> tuple[Optional[str], Optional[dict]]:
    """Build the telegram brief once and return (text, state).

    state = {"side": reconciled_side, "confidence": confidence} or None when
    data is insufficient. Used by the on-change watcher so it can diff the
    reconciled side/confidence between polls without rebuilding the snapshot.
    """
    conn = _connect()
    cur = conn.cursor()
    try:
        snap = _build_snapshot(cur, conn, pair)
        if snap is None:
            return None, None
        text = _render_telegram_markdown(snap)
        state = {"side": snap.reconciled_side, "confidence": snap.confidence}
        return text, state
    finally:
        cur.close()
        conn.close()


def cmd_snapshot(args) -> int:
    conn = _connect()
    cur = conn.cursor()
    snap = _build_snapshot(cur, conn, args.pair)
    if snap is None:
        print(f"[{args.pair}] insufficient candles")
        cur.close()
        conn.close()
        return 1
    rendered = _render(snap)
    print(rendered)
    cur.close()
    conn.close()
    if not _has_required_sections(rendered):
        print("\nERROR: output missing one or more required sections")
        return 1
    n_narr = _narrative_line_count(rendered)
    if n_narr > 5:
        print(f"\nERROR: narrative section too long ({n_narr} lines > 5)")
        return 1
    return 0


def cmd_all(args) -> int:
    conn = _connect()
    cur = conn.cursor()
    failures = []
    for pair in PAIRS:
        snap = _build_snapshot(cur, conn, pair)
        if snap is None:
            print(f"[{pair}] insufficient candles\n")
            failures.append(pair)
            continue
        rendered = _render(snap)
        print(rendered)
        print()
        if not _has_required_sections(rendered):
            print(f"ERROR: {pair} missing required sections")
            failures.append(pair)
            continue
        n_narr = _narrative_line_count(rendered)
        if n_narr > 5:
            print(f"ERROR: {pair} narrative too long ({n_narr} lines)")
            failures.append(pair)
    cur.close()
    conn.close()
    if failures:
        print(f"\nFAILED pairs: {failures}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("pair")
    p_snap.set_defaults(func=cmd_snapshot)
    p_all = sub.add_parser("all")
    p_all.set_defaults(func=cmd_all)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
