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
CANDLE_LIMIT_PER_TF = {"4h": 500, "1h": 300, "30m": 300, "15m": 300, "5m": 100}
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
    for tf in CASCADE_TFS + ["5m"]:
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
    lag_sec = max(0, int(time.time() - snap.current_time_ms / 1000))
    lag_min = lag_sec // 60
    lag_flag = (
        "✅" if lag_min <= FRESHNESS_OK_MIN
        else ("⚠️" if lag_min <= FRESHNESS_WARN_MIN else "⚠️ STALE")
    )

    # Header
    lines.append(f"*{pair}* — {ts_str} (lag {lag_min}m {lag_flag})")
    lines.append(f"Price: `{price:.6g}`")
    lines.append("")

    # BIAS
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
    lines.append(
        f"*BIAS:* {side_emoji} {side.upper()} — _{conf}_ "
        f"({score}/{total_weight})"
    )

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

    # PD Array position (4H range)
    if htf is not None:
        candles_4h = snap.raw_candles.get("4h", []) if hasattr(snap, "raw_candles") else []
        pd_info = None
        if candles_4h:
            pd_info = _pd_array_position(
                htf_candles=candles_4h, htf_state=htf.state,
                pair=pair, current_price=price, current_time_ms=snap.current_time_ms,
            )
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

    # IDM on last BOS
    if htf is not None:
        idm = _inducement_check(htf)
        if idm["has_idm"]:
            lines.append(
                f"• Last BOS: 🟢 _IDM confirmed_ "
                f"(swept `{idm['idm_level']:.6g}`)"
            )
        elif htf.state.latest_break is not None:
            lines.append("• Last BOS: ⚪ _spontaneous (no IDM)_")

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

    # PLAY IDEA — reuse existing _play_idea (already has 1.5R bug fix)
    lines.append("")
    lines.append("*PLAY:*")
    for ln in _play_idea(snap)[1:]:  # skip the "PLAY IDEA:" header
        cleaned = ln.lstrip()
        if cleaned:
            lines.append(cleaned)

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
