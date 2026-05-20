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


def _play_idea(snap: Snapshot) -> list[str]:
    """Rule-based actionable plan in plain words. NOT a signal — a scenario."""
    side = snap.reconciled_side
    conf = snap.confidence
    price = snap.current_price

    # Find nearest unbroken liquidity above and below
    liq_above = None
    liq_below = None
    for tf, tfa in snap.tf_results.items():
        for lvl in tfa.liquidity:
            if lvl.swept:
                continue
            if lvl.price > price:
                if liq_above is None or lvl.price < liq_above.price:
                    liq_above = lvl
            elif lvl.price < price:
                if liq_below is None or lvl.price > liq_below.price:
                    liq_below = lvl

    lines = ["PLAY IDEA:"]
    if conf == "low" or side == "undefined":
        lines.append("  Wait. TFs split or 4H disagrees. No clean setup.")
        return lines

    if side == "short":
        # Look for sweep above to short the rejection
        if liq_above and (liq_above.price - price) / price < 0.015:
            lines.append(
                f"  Wait for sweep above {liq_above.price:.6g} "
                f"(+{(liq_above.price - price) / price * 100:.2f}%) "
                f"then short on rejection back below."
            )
        else:
            lines.append(f"  Short at market or pullback into supply zone. Bias is down.")
        if snap.invalidation_level:
            lines.append(f"  Invalidate: 4H close above {snap.invalidation_level:.6g}.")
        if liq_below:
            lines.append(
                f"  Target / partial: {liq_below.price:.6g} "
                f"({(liq_below.price - price) / price * 100:+.2f}%, sell-stops below)."
            )

    elif side == "long":
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
        if liq_above:
            lines.append(
                f"  Target / partial: {liq_above.price:.6g} "
                f"({(liq_above.price - price) / price * 100:+.2f}%, buy-stops above)."
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


def build_brief_text(pair: str, mode: str = "short") -> Optional[str]:
    """Public entry point. Build + render the brief text for a single pair.

    mode = 'short' (default): recommendation + key levels + play idea.
    mode = 'full': full multi-section technical detail.

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
        return _render_short(snap)
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
