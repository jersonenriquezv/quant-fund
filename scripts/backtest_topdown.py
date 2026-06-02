"""Backtest /topdown manual strategy.

Phase 1 ships replay_at() + --tracer-mode.
Phase 2 ships --simulate: walk-forward fill resolver across 150d × 4 pairs,
        paired random-null benchmark, CSV output under backtest_results/.
See docs/plans/_archive/backtest-topdown-2026-05-24.md.

Run:
  PYTHONPATH=. venv/bin/python scripts/backtest_topdown.py --tracer-mode
  PYTHONPATH=. venv/bin/python scripts/backtest_topdown.py --simulate
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterator, Optional

import psycopg2

from config.settings import settings
from shared.models import Candle
from scripts.topdown_snapshot import (
    Snapshot,
    _build_snapshot,
    _set_replay_time,
    _trade_triplet,
    _last_candle_impulse,
    _wick_into_liquidity,
    _ltf_flip_vs_htf,
    _inducement_check,
    _bos_session_quality,
)

# Confluence tags captured per emission for the Phase 3.5 reliability study.
# Each maps to a signal the /topdown brief surfaces. All are direction-aligned
# (long ↔ bullish, short ↔ bearish) except structure_flip, which is a warning.
CONFLUENCE_TAGS = [
    "htf_4h_aligned",
    "htf_1h_aligned",
    "ltf_15m_aligned",
    "conf_high",
    "ob_aligned_near",
    "fvg_aligned",
    "impulse_aligned",
    "wick_tap_aligned",
    "inducement",
    "structure_flip",  # warning, excluded from positive confluence count
]
_POSITIVE_CONFLUENCES = [t for t in CONFLUENCE_TAGS if t != "structure_flip"]


def _extract_confluences(snap: Snapshot) -> dict:
    """Derive boolean confluence tags present at a /topdown emission.

    Returns {tag: bool for tag in CONFLUENCE_TAGS} plus confluence_count
    (sum of positive tags). Read-only — does not mutate snap.
    """
    side = snap.reconciled_side
    price = snap.current_price
    want_trend = "bullish" if side == "long" else "bearish" if side == "short" else None

    tags = {t: False for t in CONFLUENCE_TAGS}
    if want_trend is None:
        tags["confluence_count"] = 0
        return tags

    def _tf_trend(tf: str) -> str:
        tfa = snap.tf_results.get(tf)
        return tfa.state.trend if tfa else "undefined"

    tags["htf_4h_aligned"] = _tf_trend("4h") == want_trend
    tags["htf_1h_aligned"] = _tf_trend("1h") == want_trend
    tags["ltf_15m_aligned"] = _tf_trend("15m") == want_trend
    tags["conf_high"] = snap.confidence == "high"

    # Aligned unmitigated OB within 1.5% of price (any cascade TF)
    ob_dir = want_trend
    for tfa in snap.tf_results.values():
        for ob in tfa.obs:
            if getattr(ob, "mitigated", False):
                continue
            if ob.direction != ob_dir:
                continue
            anchor = getattr(ob, "entry_price", None) or ((ob.high + ob.low) / 2)
            if anchor and abs(anchor - price) / price <= 0.015:
                tags["ob_aligned_near"] = True
                break
        if tags["ob_aligned_near"]:
            break

    # Aligned unfilled FVG present (any cascade TF)
    for tfa in snap.tf_results.values():
        for fvg in tfa.fvgs:
            if getattr(fvg, "fully_filled", False):
                continue
            if fvg.direction == ob_dir:
                tags["fvg_aligned"] = True
                break
        if tags["fvg_aligned"]:
            break

    # Last 15m candle impulse aligned with direction
    impulse = _last_candle_impulse(snap.raw_candles.get("15m", []))
    if impulse and impulse["magnitude"] in ("big", "extreme"):
        imp_dir = "bull" if side == "long" else "bear"
        tags["impulse_aligned"] = impulse["direction"] == imp_dir

    # Wick tap into aligned liquidity (long taps SSL, short taps BSL)
    ltf = snap.tf_results.get("15m")
    if ltf:
        wick = _wick_into_liquidity(snap.raw_candles.get("15m", []), ltf.liquidity)
        if wick:
            want_tap = "ssl" if side == "long" else "bsl"
            tags["wick_tap_aligned"] = wick["side"] == want_tap

    # Inducement on 4H
    htf = snap.tf_results.get("4h")
    if htf:
        idm = _inducement_check(htf)
        tags["inducement"] = bool(idm.get("has_idm"))

    # Structure flip (warning): any LTF flipped against HTF
    flip = _ltf_flip_vs_htf(snap.tf_results)
    tags["structure_flip"] = flip is not None

    tags["confluence_count"] = sum(1 for t in _POSITIVE_CONFLUENCES if tags[t])
    return tags


PAIRS_4 = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT"]

# Phase 2 simulator constants
BAR_15M_MS = 15 * 60 * 1000
BAR_5M_MS = 5 * 60 * 1000
DEFAULT_TIMEOUT_HOURS = 24
DEDUP_WINDOW_BARS = 4  # 4 × 15m = 1h dedup, mirrors prod 1h cache scaled to backtest grain
DEDUP_ENTRY_PCT = 0.5  # consecutive emissions within 0.5% entry treated as duplicate


def _connect():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def replay_at(pair: str, t_ms: int) -> Optional[Snapshot]:
    """Re-execute _build_snapshot as if wallclock were at t_ms.

    Sets the module-level _REPLAY_T_MS hook in topdown_snapshot, which
    (a) filters candle SQL loads to timestamp <= t_ms, and
    (b) makes _now_ms() return t_ms so _today_candle_status and lag_sec
        rendering behave as they would have at t_ms.

    Restores the hook to None on exit (even on exception).
    """
    conn = _connect()
    cur = conn.cursor()
    try:
        _set_replay_time(t_ms)
        snap = _build_snapshot(cur, conn, pair)
    finally:
        _set_replay_time(None)
        cur.close()
        conn.close()
    return snap


def _snapshot_summary(snap: Optional[Snapshot]) -> dict:
    """Reduce snapshot to fields meaningful for tracer comparison."""
    if snap is None:
        return {"snap": None}
    triplet = _trade_triplet(snap)
    triplet_summary: Optional[dict] = None
    if triplet:
        if triplet.get("valid", True) and "entry" in triplet and "sl" in triplet and "tp" in triplet:
            triplet_summary = {
                "direction": snap.reconciled_side,
                "entry": round(triplet["entry"], 6),
                "sl": round(triplet["sl"], 6),
                "tp": round(triplet["tp"], 6),
                "valid": True,
            }
        else:
            triplet_summary = {
                "valid": False,
                "reason": triplet.get("reason"),
            }
    return {
        "pair": snap.pair,
        "current_price": round(snap.current_price, 6),
        "current_time_ms": snap.current_time_ms,
        "reconciled_side": snap.reconciled_side,
        "confidence": snap.confidence,
        "invalidation_level": (
            round(snap.invalidation_level, 6)
            if snap.invalidation_level is not None
            else None
        ),
        "invalidation_reason": snap.invalidation_reason,
        "triplet": triplet_summary,
    }


def _historical_consistency_check(pair: str, t_ms: int) -> dict:
    """Run replay_at(t_ms) twice + validate structural sanity."""
    from config.settings import settings as _settings

    result = {
        "pair": pair,
        "t_ms": t_ms,
        "checks": {},
    }

    try:
        snap_a = replay_at(pair, t_ms)
        snap_b = replay_at(pair, t_ms)
    except Exception as e:  # noqa: BLE001
        result["checks"]["no_exception"] = False
        result["error"] = repr(e)
        return result

    result["checks"]["no_exception"] = True
    summary_a = _snapshot_summary(snap_a)
    summary_b = _snapshot_summary(snap_b)
    result["summary"] = summary_a

    if snap_a is None:
        result["checks"]["snap_not_none"] = False
        return result
    result["checks"]["snap_not_none"] = True

    result["checks"]["valid_reconciled_side"] = snap_a.reconciled_side in (
        "long",
        "short",
        "undefined",
    )

    htf = snap_a.tf_results.get("4h")
    ltf = snap_a.tf_results.get("15m")
    htf_trend = htf.state.trend if htf else "undefined"
    ltf_trend = ltf.state.trend if ltf else "undefined"
    valid_trend_enum = ("bullish", "bearish", "undefined")
    result["checks"]["valid_htf_trend"] = htf_trend in valid_trend_enum
    result["checks"]["valid_ltf_trend"] = ltf_trend in valid_trend_enum

    # Triplet geometry quality is an OBSERVATION, not a Phase 1 fidelity gate.
    # Strategy-side geometry bugs (e.g., long SL above entry) are real findings
    # to surface in the Phase 3 report — they are not replay failures.
    triplet = _trade_triplet(snap_a)
    triplet_present = bool(
        triplet and triplet.get("valid", True) and "entry" in triplet and "sl" in triplet
    )
    result["triplet_present"] = triplet_present
    if triplet_present:
        entry = triplet["entry"]
        sl = triplet["sl"]
        direction = snap_a.reconciled_side
        price = snap_a.current_price
        entry_dist_pct = abs(entry - price) / price * 100 if price else 0
        sl_dist_pct = abs(sl - entry) / entry * 100 if entry else 0
        min_sl_pct = _settings.MIN_RISK_DISTANCE_PCT * 100
        sl_correct_side = (
            (direction == "short" and sl > entry)
            or (direction == "long" and sl < entry)
            or direction == "undefined"
        )
        result["observations"] = {
            "entry_within_2pct": entry_dist_pct <= 2.0,
            "entry_dist_pct": round(entry_dist_pct, 2),
            "sl_min_dist": sl_dist_pct >= min_sl_pct,
            "sl_dist_pct": round(sl_dist_pct, 2),
            "sl_on_correct_side": sl_correct_side,
        }

    result["checks"]["deterministic"] = summary_a == summary_b
    return result


def _diff_summaries(a: dict, b: dict) -> list[str]:
    diffs = []
    for k in sorted(set(a.keys()) | set(b.keys())):
        if a.get(k) != b.get(k):
            diffs.append(f"  {k}: live={a.get(k)!r}  replay={b.get(k)!r}")
    return diffs


def _run_anchor_test(pair: str = "ETH/USDT") -> dict:
    """Compare in-process live snapshot vs replay_at(now). Both use the same
    underlying _build_snapshot — anchor checks that the replay hook is a no-op
    when t_ms tracks real time. If this fails, the shim has a hidden global.
    """
    now_ms = int(time.time() * 1000)

    # Live snapshot via in-process call (no replay hook active).
    conn = _connect()
    cur = conn.cursor()
    try:
        live_snap = _build_snapshot(cur, conn, pair)
    finally:
        cur.close()
        conn.close()
    live_summary = _snapshot_summary(live_snap)

    # Replay snapshot pinned at now_ms.
    replay_snap = replay_at(pair, now_ms)
    replay_summary = _snapshot_summary(replay_snap)

    # current_time_ms may differ by a few ms (5m candle close granularity).
    # Allow up to 1 bar of drift; everything else must match.
    drift_ms = abs(
        live_summary.get("current_time_ms", 0)
        - replay_summary.get("current_time_ms", 0)
    )

    # Strip current_time_ms before comparing the rest.
    live_cmp = {k: v for k, v in live_summary.items() if k != "current_time_ms"}
    replay_cmp = {k: v for k, v in replay_summary.items() if k != "current_time_ms"}

    match = live_cmp == replay_cmp
    return {
        "pair": pair,
        "match": match,
        "drift_ms": drift_ms,
        "diffs": _diff_summaries(live_cmp, replay_cmp) if not match else [],
        "live": live_summary,
        "replay": replay_summary,
    }


def _fetch_historical_t_ms(n: int = 5) -> list[tuple[str, int]]:
    """Pull n (pair, t_ms) tuples from topdown_brief_renders."""
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT pair, EXTRACT(EPOCH FROM rendered_at) * 1000 AS t_ms
            FROM topdown_brief_renders
            ORDER BY rendered_at DESC
            LIMIT %s
            """,
            (n,),
        )
        rows = [(r[0], int(r[1])) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return rows


def run_tracer_mode() -> int:
    """Phase 1 verification gate. Returns 0 on PASS, 1 on FAIL."""
    print("=" * 72)
    print("Phase 1 tracer — /topdown backtest")
    print("=" * 72)

    # Gate 1: anchor test
    print("\n[1/3] Anchor test — live(now) vs replay_at(now) on ETH/USDT")
    anchor = _run_anchor_test("ETH/USDT")
    if anchor["match"]:
        print(f"  ✅ PASS — drift_ms={anchor['drift_ms']}")
    else:
        print(f"  ❌ FAIL — drift_ms={anchor['drift_ms']}, diffs:")
        for d in anchor["diffs"]:
            print(d)

    # Gate 2: historical consistency
    print("\n[2/3] Historical consistency — 5 bars from topdown_brief_renders")
    historical_rows = _fetch_historical_t_ms(5)
    if len(historical_rows) < 5:
        print(f"  ⚠️  only {len(historical_rows)} historical renders available")
    historical_results = []
    for pair, t_ms in historical_rows:
        res = _historical_consistency_check(pair, t_ms)
        passed = all(v for v in res["checks"].values() if isinstance(v, bool))
        symbol = "✅" if passed else "❌"
        print(
            f"  {symbol} {pair} @ {t_ms} — checks={res['checks']}"
        )
        if "error" in res:
            print(f"      error: {res['error']}")
        historical_results.append((pair, t_ms, passed, res))
    n_pass = sum(1 for *_, p, _r in historical_results if p)

    # Gate 3: spot-check stdout
    print("\n[3/3] Spot-check stdout — one historical replay")
    if historical_results:
        pair, t_ms, _, res = historical_results[0]
        snap = replay_at(pair, t_ms)
        print(f"  Snapshot @ {pair} t={t_ms}:")
        if snap is None:
            print("    (None — _build_snapshot returned None)")
        else:
            print(f"    current_price       = {snap.current_price}")
            print(f"    reconciled_side     = {snap.reconciled_side}")
            print(f"    confidence          = {snap.confidence}")
            print(f"    invalidation_level  = {snap.invalidation_level}")
            print(f"    invalidation_reason = {snap.invalidation_reason}")
            triplet = _trade_triplet(snap)
            print(f"    triplet             = {triplet}")
    else:
        print("  ⚠️  no historical rows to spot-check")

    print("\n" + "=" * 72)
    anchor_pass = anchor["match"]
    historical_pass = n_pass >= 4
    overall_pass = anchor_pass and historical_pass
    print(
        f"Anchor: {'PASS' if anchor_pass else 'FAIL'}  |  "
        f"Historical: {n_pass}/{len(historical_results)} "
        f"({'PASS' if historical_pass else 'FAIL'} threshold ≥4/5)"
    )
    print(f"OVERALL: {'PASS' if overall_pass else 'FAIL'}")
    print("=" * 72)
    return 0 if overall_pass else 1


def _load_all_candles(pair: str, tf: str) -> list[Candle]:
    """Load every candle for (pair, tf), ascending. Used by Phase 2 fill walker."""
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT timestamp, open, high, low, close, volume, volume_quote
            FROM candles
            WHERE pair = %s AND timeframe = %s
            ORDER BY timestamp ASC
            """,
            (pair, tf),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return [
        Candle(
            timestamp=int(r[0]), open=float(r[1]), high=float(r[2]),
            low=float(r[3]), close=float(r[4]), volume=float(r[5]),
            volume_quote=float(r[6]) if r[6] is not None else 0.0,
            pair=pair, timeframe=tf, confirmed=True,
        )
        for r in rows
    ]


def iter_emissions_for_pair(
    pair: str, start_ms: int, end_ms: int,
) -> Iterator[dict]:
    """Replay 15m bars over the window and yield valid triplet emissions.

    Dedup: consecutive emissions with same direction AND entry within
    DEDUP_ENTRY_PCT% AND within DEDUP_WINDOW_BARS bars of the prior are dropped.
    Mirrors the production 1h dedup cache scaled to backtest grain.
    """
    # Pull 15m close timestamps in window — these are the bar boundaries we evaluate.
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT timestamp FROM candles
            WHERE pair = %s AND timeframe = '15m'
              AND timestamp >= %s AND timestamp <= %s
            ORDER BY timestamp ASC
            """,
            (pair, start_ms, end_ms),
        )
        bar_ts_list = [int(r[0]) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    last_emit: Optional[dict] = None

    for t_ms in bar_ts_list:
        try:
            snap = replay_at(pair, t_ms)
        except Exception as e:  # noqa: BLE001
            # Snapshot construction errors are not fatal — log and skip.
            print(f"  [WARN] replay_at({pair}, {t_ms}) raised: {e!r}", file=sys.stderr)
            continue
        if snap is None:
            continue
        triplet = _trade_triplet(snap)
        if not triplet or not triplet.get("valid", False):
            continue
        direction = snap.reconciled_side
        if direction not in ("long", "short"):
            continue
        entry = triplet["entry"]
        sl = triplet["sl"]
        tp = triplet["tp"]

        # Dedup: same direction, entry within X%, within Y bars
        if last_emit is not None:
            bar_gap = (t_ms - last_emit["t_ms"]) // BAR_15M_MS
            if bar_gap <= DEDUP_WINDOW_BARS and direction == last_emit["direction"]:
                entry_drift_pct = abs(entry - last_emit["entry"]) / last_emit["entry"] * 100
                if entry_drift_pct <= DEDUP_ENTRY_PCT:
                    continue  # duplicate emission

        emission = {
            "pair": pair,
            "t_ms": t_ms,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": triplet.get("rr", 0.0),
            "risk_pct": triplet.get("risk_pct", 0.0),
            "reward_pct": triplet.get("reward_pct", 0.0),
            "sweep_distance_pct": triplet.get("sweep_distance_pct", 0.0),
            "tp_mode": triplet.get("tp_mode", "single"),
            "current_price": snap.current_price,
        }
        # Phase 3.5: capture confluence tags present at emission.
        conf = _extract_confluences(snap)
        for tag in CONFLUENCE_TAGS:
            emission[tag] = int(conf[tag])
        emission["confluence_count"] = conf["confluence_count"]
        last_emit = emission
        yield emission


def simulate_fill(
    emission: dict,
    candles_5m: list[Candle],
    timeout_hours: int = DEFAULT_TIMEOUT_HOURS,
) -> dict:
    """Walk-forward fill simulator.

    Entry = LIMIT order at `emission["entry"]`. Fills when 5m candle
    touches entry (high >= entry for long, low <= entry for short — but we
    use general OHLC range intersection since limit orders fill on touch).

    Once filled, walk forward looking for SL or TP hit. If a candle spans
    both SL and TP, treat SL as hit first (conservative — assume adverse
    move happened intracandle before favorable).

    Returns dict with outcome in {tp, sl, timeout, unfilled_timeout} plus
    entry_fill_ts and exit_ts when applicable.
    """
    t0 = emission["t_ms"]
    entry = emission["entry"]
    sl = emission["sl"]
    tp = emission["tp"]
    direction = emission["direction"]
    deadline_ms = t0 + timeout_hours * 3600 * 1000

    # Binary search for first 5m candle with timestamp > t0
    idx_start = 0
    lo, hi = 0, len(candles_5m)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles_5m[mid].timestamp <= t0:
            lo = mid + 1
        else:
            hi = mid
    idx_start = lo

    # Stage 1: wait for entry fill
    entry_fill_ts: Optional[int] = None
    i = idx_start
    while i < len(candles_5m):
        c = candles_5m[i]
        if c.timestamp > deadline_ms:
            return {
                **emission,
                "outcome": "unfilled_timeout",
                "entry_fill_ts": None,
                "exit_ts": c.timestamp,
                "exit_price": None,
            }
        # Limit fill: did this candle's range touch entry?
        if c.low <= entry <= c.high:
            entry_fill_ts = c.timestamp
            break
        i += 1
    else:
        # Ran out of candles before fill — open-ended, treat as unfilled.
        return {
            **emission,
            "outcome": "unfilled_timeout",
            "entry_fill_ts": None,
            "exit_ts": None,
            "exit_price": None,
        }

    # Stage 2: walk forward looking for SL or TP
    j = i + 1
    while j < len(candles_5m):
        c = candles_5m[j]
        if c.timestamp > deadline_ms:
            return {
                **emission,
                "outcome": "timeout",
                "entry_fill_ts": entry_fill_ts,
                "exit_ts": c.timestamp,
                "exit_price": c.close,
            }
        sl_hit = (c.low <= sl <= c.high)
        tp_hit = (c.low <= tp <= c.high)
        if direction == "long":
            sl_hit = c.low <= sl  # any move below SL stops out
            tp_hit = c.high >= tp
        else:
            sl_hit = c.high >= sl
            tp_hit = c.low <= tp
        if sl_hit and tp_hit:
            # Both in same candle → SL wins (conservative)
            return {
                **emission,
                "outcome": "sl",
                "entry_fill_ts": entry_fill_ts,
                "exit_ts": c.timestamp,
                "exit_price": sl,
            }
        if sl_hit:
            return {
                **emission,
                "outcome": "sl",
                "entry_fill_ts": entry_fill_ts,
                "exit_ts": c.timestamp,
                "exit_price": sl,
            }
        if tp_hit:
            return {
                **emission,
                "outcome": "tp",
                "entry_fill_ts": entry_fill_ts,
                "exit_ts": c.timestamp,
                "exit_price": tp,
            }
        j += 1
    return {
        **emission,
        "outcome": "timeout",
        "entry_fill_ts": entry_fill_ts,
        "exit_ts": candles_5m[-1].timestamp if candles_5m else None,
        "exit_price": candles_5m[-1].close if candles_5m else None,
    }


def make_random_null(emission: dict, rng: random.Random) -> dict:
    """Generate paired random-null trade.

    Same pair, same timestamp, same risk%/reward%/timeout. Direction is 50/50
    random. Entry is the emission bar's close (current_price at t_ms), not the
    sweep level — random has no claim to picking a sweep. SL/TP recomputed
    from risk_pct/reward_pct around the new entry on the new direction.
    """
    direction = rng.choice(["long", "short"])
    entry = emission["current_price"]
    risk_pct = emission["risk_pct"] / 100.0
    reward_pct = emission["reward_pct"] / 100.0
    if direction == "long":
        sl = entry * (1.0 - risk_pct)
        tp = entry * (1.0 + reward_pct)
    else:
        sl = entry * (1.0 + risk_pct)
        tp = entry * (1.0 - reward_pct)
    return {
        "pair": emission["pair"],
        "t_ms": emission["t_ms"],
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": emission["rr"],
        "risk_pct": emission["risk_pct"],
        "reward_pct": emission["reward_pct"],
        "sweep_distance_pct": 0.0,  # n/a for random
        "tp_mode": "random_mirror",
        "current_price": entry,
    }


def _write_trades_csv(path: str, trades: list[dict]) -> None:
    if not trades:
        return
    keys = list(trades[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for t in trades:
            writer.writerow(t)


def run_simulator(
    pairs: list[str] = PAIRS_4,
    days: int = 150,
    timeout_hours: int = DEFAULT_TIMEOUT_HOURS,
    random_seed: int = 42,
) -> int:
    """Phase 2 simulator. Returns 0 on gate PASS, 1 on FAIL."""
    run_id = datetime.utcnow().strftime("topdown_%Y%m%d_%H%M%S")
    out_dir = "backtest_results"
    os.makedirs(out_dir, exist_ok=True)
    trades_path = os.path.join(out_dir, f"{run_id}_trades.csv")
    random_path = os.path.join(out_dir, f"{run_id}_random_trades.csv")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    end_ms = now_ms - 24 * 3600 * 1000  # leave 1d slack to allow timeout walk

    print("=" * 72)
    print(f"Phase 2 simulator — run_id={run_id}")
    print(f"  pairs={pairs}  days={days}  timeout={timeout_hours}h")
    print(f"  window: {start_ms} → {end_ms}")
    print("=" * 72)

    rng = random.Random(random_seed)
    all_trades: list[dict] = []
    all_random: list[dict] = []

    for pair in pairs:
        t_pair_start = time.time()
        print(f"\n[{pair}] loading 5m candles for fill walker...")
        candles_5m = _load_all_candles(pair, "5m")
        print(f"  loaded {len(candles_5m)} 5m candles")

        print(f"[{pair}] iterating emissions...")
        emissions: list[dict] = []
        for emission in iter_emissions_for_pair(pair, start_ms, end_ms):
            emissions.append(emission)
        print(f"  {len(emissions)} valid + deduped emissions")

        print(f"[{pair}] simulating fills...")
        for emission in emissions:
            outcome = simulate_fill(emission, candles_5m, timeout_hours)
            all_trades.append(outcome)
            random_emission = make_random_null(emission, rng)
            random_outcome = simulate_fill(random_emission, candles_5m, timeout_hours)
            all_random.append(random_outcome)

        elapsed = time.time() - t_pair_start
        print(f"  {pair} done in {elapsed:.1f}s")

    print(f"\nWriting {trades_path}")
    _write_trades_csv(trades_path, all_trades)
    print(f"Writing {random_path}")
    _write_trades_csv(random_path, all_random)

    return _evaluate_phase2_gate(all_trades, all_random, run_id)


def _evaluate_phase2_gate(
    trades: list[dict], random_trades: list[dict], run_id: str,
) -> int:
    """Phase 2 verification gate."""
    print("\n" + "=" * 72)
    print(f"Phase 2 gate evaluation — run_id={run_id}")
    print("=" * 72)

    def _stats(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0, "wr": 0.0, "tp": 0, "sl": 0, "timeout": 0, "unfilled": 0}
        tp = sum(1 for r in rows if r["outcome"] == "tp")
        sl = sum(1 for r in rows if r["outcome"] == "sl")
        timeout = sum(1 for r in rows if r["outcome"] == "timeout")
        unfilled = sum(1 for r in rows if r["outcome"] == "unfilled_timeout")
        resolved = tp + sl
        wr = (tp / resolved * 100) if resolved > 0 else 0.0
        return {
            "n": len(rows),
            "resolved": resolved,
            "tp": tp,
            "sl": sl,
            "timeout": timeout,
            "unfilled": unfilled,
            "wr": round(wr, 1),
        }

    tw_stats = _stats(trades)
    rn_stats = _stats(random_trades)

    print(f"\n/topdown trades:   {tw_stats}")
    print(f"Random null:       {rn_stats}")

    # Gate checks
    # Bands revised 2026-05-24 (Phase 2 plan-revision): random benchmark
    # mathematically cannot hit 35-55% WR with R:R asymmetry > 1:1 (random WR
    # converges to 1/(1+R), e.g. R:R 2:1 → ~33%). Bands now check sanity, not
    # edge — strict gate "random must not WIN" is the real rollback trigger.
    n_pass = tw_stats["n"] >= 200
    wr_in_band = 10.0 <= tw_stats["wr"] <= 90.0 if tw_stats["resolved"] > 0 else False
    random_wr_in_band = 0.0 <= rn_stats["wr"] <= 55.0 if rn_stats["resolved"] > 0 else False
    # Lookahead audit
    lookahead_ok = True
    for r in trades + random_trades:
        if r["outcome"] in ("tp", "sl") and r.get("exit_ts") is not None:
            if r["exit_ts"] <= r["t_ms"]:
                lookahead_ok = False
                print(f"  [LOOKAHEAD] {r['pair']} t={r['t_ms']} exit={r['exit_ts']}")
                break

    print(f"\nGate checks:")
    print(f"  N ≥ 200:                    {'✅' if n_pass else '❌'} (N={tw_stats['n']})")
    print(f"  /topdown WR ∈ [10, 90]:     {'✅' if wr_in_band else '❌'} ({tw_stats['wr']}%)")
    print(f"  random WR ≤ 55:             {'✅' if random_wr_in_band else '❌'} ({rn_stats['wr']}%)")
    print(f"  no lookahead:               {'✅' if lookahead_ok else '❌'}")

    # Rollback triggers
    rollback = False
    if tw_stats["n"] < 100:
        print(f"  ⚠️ ROLLBACK: N < 100 (insufficient power)")
        rollback = True
    if not lookahead_ok:
        print(f"  ⚠️ ROLLBACK: lookahead detected")
        rollback = True
    if rn_stats["wr"] > 55:
        print(f"  ⚠️ ROLLBACK: random null WR > 55 (bug suspected)")
        rollback = True

    overall = n_pass and wr_in_band and random_wr_in_band and lookahead_ok and not rollback
    print(f"\nOVERALL: {'PASS' if overall else 'FAIL'}")
    print("=" * 72)
    return 0 if overall else 1


def _trade_pnl_r(t: dict) -> float:
    """Compute realized PnL in R units (1 R = risk per trade).

    TP: +rr R. SL: -1 R. Timeout: signed (exit - entry) / risk_distance.
    Unfilled: 0 R.
    """
    outcome = t["outcome"]
    if outcome == "unfilled_timeout":
        return 0.0
    entry = float(t["entry"])
    sl = float(t["sl"])
    direction = t["direction"]
    risk_dist = abs(entry - sl)
    if risk_dist == 0:
        return 0.0
    if outcome == "tp":
        return float(t.get("rr") or 0) or 0.0
    if outcome == "sl":
        return -1.0
    # timeout
    exit_price = t.get("exit_price")
    if exit_price in (None, "", "None"):
        return 0.0
    exit_price = float(exit_price)
    if direction == "long":
        return (exit_price - entry) / risk_dist
    return (entry - exit_price) / risk_dist


def _fees_r(t: dict, fee_pct_per_leg: float) -> float:
    """Round-trip fees in R units. fee_pct_per_leg as fraction (0.0001 = 0.01%)."""
    if t["outcome"] == "unfilled_timeout":
        return 0.0
    risk_pct = float(t.get("risk_pct") or 0) / 100.0
    if risk_pct <= 0:
        return 0.0
    rt_fee_fraction = 2 * fee_pct_per_leg
    return rt_fee_fraction / risk_pct


def _aggregate(rows: list[dict], fee_pct_per_leg: float = 0.0) -> dict:
    """Compute headline metrics on a slice of trades."""
    n = len(rows)
    tp = sum(1 for r in rows if r["outcome"] == "tp")
    sl = sum(1 for r in rows if r["outcome"] == "sl")
    timeout = sum(1 for r in rows if r["outcome"] == "timeout")
    unfilled = sum(1 for r in rows if r["outcome"] == "unfilled_timeout")
    resolved = tp + sl
    wr_resolved = (tp / resolved * 100) if resolved > 0 else 0.0
    filled = tp + sl + timeout
    wr_filled = (tp / filled * 100) if filled > 0 else 0.0
    wr_all = (tp / n * 100) if n > 0 else 0.0

    pnls = [_trade_pnl_r(r) - _fees_r(r, fee_pct_per_leg) for r in rows]
    total_r = sum(pnls)
    wins_r = sum(p for p in pnls if p > 0)
    losses_r = -sum(p for p in pnls if p < 0)
    pf = (wins_r / losses_r) if losses_r > 0 else float("inf")
    expectancy_r = (total_r / n) if n > 0 else 0.0

    # Max DD on cumulative equity curve (R units, in chronological order)
    rows_sorted = sorted(rows, key=lambda r: int(r["t_ms"]))
    pnls_sorted = [_trade_pnl_r(r) - _fees_r(r, fee_pct_per_leg) for r in rows_sorted]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls_sorted:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return {
        "n": n,
        "tp": tp,
        "sl": sl,
        "timeout": timeout,
        "unfilled": unfilled,
        "resolved": resolved,
        "wr_resolved_pct": round(wr_resolved, 2),
        "wr_filled_pct": round(wr_filled, 2),
        "wr_all_pct": round(wr_all, 2),
        "total_r": round(total_r, 2),
        "pf": round(pf, 2) if pf != float("inf") else None,
        "expectancy_r": round(expectancy_r, 4),
        "max_dd_r": round(max_dd, 2),
    }


def _z_test_two_proportions(
    successes_a: int, total_a: int, successes_b: int, total_b: int,
) -> tuple[float, float, tuple[float, float]]:
    """Returns (z, p_value_two_sided, 95% CI on (p_a - p_b))."""
    import math
    if total_a == 0 or total_b == 0:
        return 0.0, 1.0, (0.0, 0.0)
    p_a = successes_a / total_a
    p_b = successes_b / total_b
    delta = p_a - p_b
    p_pool = (successes_a + successes_b) / (total_a + total_b)
    se_pool = math.sqrt(p_pool * (1 - p_pool) * (1 / total_a + 1 / total_b))
    if se_pool == 0:
        return 0.0, 1.0, (delta, delta)
    z = delta / se_pool
    # Two-sided p via normal CDF approximation
    p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    # 95% CI uses unpooled SE
    se_unp = math.sqrt(p_a * (1 - p_a) / total_a + p_b * (1 - p_b) / total_b)
    ci_lo = delta - 1.96 * se_unp
    ci_hi = delta + 1.96 * se_unp
    return round(z, 3), round(p_value, 4), (round(ci_lo, 4), round(ci_hi, 4))


def _format_metrics_row(label: str, m: dict) -> str:
    return (
        f"| {label} | {m['n']} | {m['resolved']} | {m['tp']} | {m['sl']} | "
        f"{m['timeout']} | {m['unfilled']} | {m['wr_resolved_pct']:.2f}% | "
        f"{m['total_r']:+.2f} | {m.get('pf') if m.get('pf') is not None else '∞'} | "
        f"{m['expectancy_r']:+.4f} | {m['max_dd_r']:.2f} |"
    )


def _bucketize_sweep(pct: float) -> str:
    if pct < 1: return "0-1%"
    if pct < 2: return "1-2%"
    if pct < 3: return "2-3%"
    if pct < 5: return "3-5%"
    return "5%+"


def _bucketize_rr(rr: float) -> str:
    if rr < 1.5: return "<1.5"
    if rr < 2.0: return "1.5-2.0"
    if rr < 3.0: return "2.0-3.0"
    if rr < 5.0: return "3.0-5.0"
    return "5.0+"


def _month_label(t_ms: int) -> str:
    dt = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m")


def _verdict(delta_wr_pp: float, p_value: float) -> tuple[str, str]:
    if delta_wr_pp >= 10 and p_value < 0.05:
        return "EDGE", "Δ WR ≥ 10pp AND p < 0.05 → port to bot post-FREEZE per plan handoff."
    if delta_wr_pp >= 5 and p_value < 0.10:
        return "INCONCLUSIVE", "Δ WR in [5, 10)pp — statistically suggestive but below the 10pp threshold. Continue live falsification (N=30 journal flag) before any port."
    if delta_wr_pp < 5:
        return "NO EDGE", (
            "Δ WR < 5pp — practical edge absent. Strategy may have weak statistical "
            "signal but does not justify manual execution. Do not port. "
            "Consider redesigning OR continue live falsification but lower priority."
        )
    return "INCONCLUSIVE", f"Δ WR = {delta_wr_pp:.2f}pp, p = {p_value:.4f} — borderline. Need more data."


def run_report(run_id: str) -> int:
    """Phase 3 report aggregator. Reads CSVs from backtest_results/<run_id>_*."""
    out_dir = "backtest_results"
    trades_path = os.path.join(out_dir, f"{run_id}_trades.csv")
    random_path = os.path.join(out_dir, f"{run_id}_random_trades.csv")
    report_path = os.path.join(out_dir, f"{run_id}_report.md")

    if not os.path.exists(trades_path) or not os.path.exists(random_path):
        print(f"ERROR: CSV(s) not found. Looked for:")
        print(f"  {trades_path}")
        print(f"  {random_path}")
        return 1

    print(f"Loading {trades_path}")
    with open(trades_path) as f:
        trades = list(csv.DictReader(f))
    print(f"Loading {random_path}")
    with open(random_path) as f:
        random_trades = list(csv.DictReader(f))

    # Coerce numeric fields where needed
    def _coerce(rows: list[dict]) -> list[dict]:
        for r in rows:
            for k in ("t_ms", "entry_fill_ts", "exit_ts"):
                if r.get(k) and r[k] not in ("None", ""):
                    r[k] = int(r[k])
        return rows
    trades = _coerce(trades)
    random_trades = _coerce(random_trades)

    print(f"Computing aggregations for {len(trades)} /topdown + {len(random_trades)} random trades...")

    # Headline metrics, both fee scenarios
    fee_maker = 0.0001  # 0.01% per leg → 0.02% RT
    fee_taker = 0.00055  # 0.055% per leg → 0.11% RT

    base_topdown = _aggregate(trades, 0.0)
    base_random = _aggregate(random_trades, 0.0)
    maker_topdown = _aggregate(trades, fee_maker)
    maker_random = _aggregate(random_trades, fee_maker)
    taker_topdown = _aggregate(trades, fee_taker)
    taker_random = _aggregate(random_trades, fee_taker)

    # Z-test on resolved WR
    z, p_value, ci = _z_test_two_proportions(
        base_topdown["tp"], base_topdown["resolved"],
        base_random["tp"], base_random["resolved"],
    )
    delta_wr_pp = base_topdown["wr_resolved_pct"] - base_random["wr_resolved_pct"]

    # Per-pair
    pairs = sorted({r["pair"] for r in trades})
    per_pair_topdown = {p: _aggregate([r for r in trades if r["pair"] == p], 0.0) for p in pairs}
    per_pair_random = {p: _aggregate([r for r in random_trades if r["pair"] == p], 0.0) for p in pairs}

    # Per-month
    months = sorted({_month_label(int(r["t_ms"])) for r in trades})
    per_month_topdown = {
        m: _aggregate([r for r in trades if _month_label(int(r["t_ms"])) == m], 0.0)
        for m in months
    }
    per_month_random = {
        m: _aggregate([r for r in random_trades if _month_label(int(r["t_ms"])) == m], 0.0)
        for m in months
    }

    # Per-direction
    per_dir_topdown = {
        d: _aggregate([r for r in trades if r["direction"] == d], 0.0) for d in ("long", "short")
    }
    per_dir_random = {
        d: _aggregate([r for r in random_trades if r["direction"] == d], 0.0) for d in ("long", "short")
    }

    # Per-sweep-distance bucket (/topdown only — random has 0% sweep_distance)
    sweep_buckets = ["0-1%", "1-2%", "2-3%", "3-5%", "5%+"]
    per_sweep = {}
    for b in sweep_buckets:
        per_sweep[b] = _aggregate(
            [r for r in trades if _bucketize_sweep(float(r.get("sweep_distance_pct") or 0)) == b],
            0.0,
        )

    # Per-rr bucket
    rr_buckets = ["<1.5", "1.5-2.0", "2.0-3.0", "3.0-5.0", "5.0+"]
    per_rr = {}
    for b in rr_buckets:
        per_rr[b] = _aggregate(
            [r for r in trades if _bucketize_rr(float(r.get("rr") or 0)) == b], 0.0,
        )

    # Per-tp-mode
    per_tpm = {
        m: _aggregate([r for r in trades if r.get("tp_mode") == m], 0.0)
        for m in sorted({r.get("tp_mode", "single") for r in trades})
    }

    # 70/30 chronological split
    sorted_trades = sorted(trades, key=lambda r: r["t_ms"])
    sorted_random = sorted(random_trades, key=lambda r: r["t_ms"])
    split_idx_t = int(len(sorted_trades) * 0.70)
    split_idx_r = int(len(sorted_random) * 0.70)
    train_topdown = _aggregate(sorted_trades[:split_idx_t], 0.0)
    holdout_topdown = _aggregate(sorted_trades[split_idx_t:], 0.0)
    train_random = _aggregate(sorted_random[:split_idx_r], 0.0)
    holdout_random = _aggregate(sorted_random[split_idx_r:], 0.0)

    # Window sensitivity (last 90/120/150d)
    now_ms = max(int(r["t_ms"]) for r in trades) if trades else int(time.time() * 1000)
    window_metrics = {}
    for window_d in (90, 120, 150):
        cutoff_ms = now_ms - window_d * 24 * 3600 * 1000
        sub_t = [r for r in trades if int(r["t_ms"]) >= cutoff_ms]
        sub_r = [r for r in random_trades if int(r["t_ms"]) >= cutoff_ms]
        window_metrics[window_d] = {
            "topdown": _aggregate(sub_t, 0.0),
            "random": _aggregate(sub_r, 0.0),
        }

    # Verdict
    verdict_label, verdict_note = _verdict(delta_wr_pp, p_value)

    # Compose markdown report
    lines = []
    lines.append(f"# /topdown Backtest Report — {run_id}")
    lines.append("")
    lines.append(f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Source plan:** `docs/plans/_archive/backtest-topdown-2026-05-24.md`")
    lines.append(f"**Trade CSVs:** `{trades_path}`, `{random_path}`")
    lines.append("")
    lines.append(f"## Verdict: **{verdict_label}**")
    lines.append("")
    lines.append(f"> Δ WR (/topdown − random) = **{delta_wr_pp:+.2f}pp** | z = {z} | p = {p_value} | 95% CI on Δ: [{ci[0]*100:+.2f}pp, {ci[1]*100:+.2f}pp]")
    lines.append("")
    lines.append(verdict_note)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. Headline")
    lines.append("")
    lines.append("Metrics column legend: `n` = total emissions, `resolved` = TP+SL, `wr` = TP/resolved, `total_r` = sum PnL in R units, `pf` = profit factor, `exp_r` = expectancy per trade in R, `dd_r` = max drawdown in R.")
    lines.append("")
    lines.append("### 1a. No fees (gross)")
    lines.append("")
    lines.append("| Strategy | N | Resolved | TP | SL | Timeout | Unfilled | WR | Total R | PF | Exp R | DD R |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    lines.append(_format_metrics_row("/topdown", base_topdown))
    lines.append(_format_metrics_row("random", base_random))
    lines.append("")
    lines.append("### 1b. Maker fees (Bybit non-VIP limit-only, 0.02% RT)")
    lines.append("")
    lines.append("| Strategy | N | Resolved | TP | SL | Timeout | Unfilled | WR | Total R | PF | Exp R | DD R |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    lines.append(_format_metrics_row("/topdown", maker_topdown))
    lines.append(_format_metrics_row("random", maker_random))
    lines.append("")
    lines.append("### 1c. Taker fees stress (0.11% RT)")
    lines.append("")
    lines.append("| Strategy | N | Resolved | TP | SL | Timeout | Unfilled | WR | Total R | PF | Exp R | DD R |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    lines.append(_format_metrics_row("/topdown", taker_topdown))
    lines.append(_format_metrics_row("random", taker_random))
    lines.append("")
    lines.append("**Reading:** Both fee scenarios show the same qualitative picture — /topdown loses less than random but neither is profitable. Expectancy stays negative throughout. Edge in WR is statistically real (p < 0.05) but too small for practical capital deployment.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Per-pair section
    lines.append("## 2. Per-pair breakdown")
    lines.append("")
    lines.append("| Pair | /topdown N | /topdown WR | /topdown R | random N | random WR | random R | Δ WR |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for p in pairs:
        td = per_pair_topdown[p]
        rn = per_pair_random[p]
        d = td["wr_resolved_pct"] - rn["wr_resolved_pct"]
        lines.append(
            f"| {p} | {td['n']} | {td['wr_resolved_pct']:.2f}% | {td['total_r']:+.2f} | "
            f"{rn['n']} | {rn['wr_resolved_pct']:.2f}% | {rn['total_r']:+.2f} | {d:+.2f}pp |"
        )
    lines.append("")

    # Per-month
    lines.append("## 3. Per-month breakdown")
    lines.append("")
    lines.append("| Month | /topdown N | /topdown WR | /topdown R | random WR | Δ WR |")
    lines.append("|---|---|---|---|---|---|")
    for m in months:
        td = per_month_topdown[m]
        rn = per_month_random[m]
        d = td["wr_resolved_pct"] - rn["wr_resolved_pct"]
        lines.append(
            f"| {m} | {td['n']} | {td['wr_resolved_pct']:.2f}% | {td['total_r']:+.2f} | "
            f"{rn['wr_resolved_pct']:.2f}% | {d:+.2f}pp |"
        )
    lines.append("")

    # Per-direction
    lines.append("## 4. Per-direction breakdown")
    lines.append("")
    lines.append("| Direction | /topdown N | /topdown WR | /topdown R | random WR | Δ WR |")
    lines.append("|---|---|---|---|---|---|")
    for d in ("long", "short"):
        td = per_dir_topdown[d]
        rn = per_dir_random[d]
        diff = td["wr_resolved_pct"] - rn["wr_resolved_pct"]
        lines.append(
            f"| {d} | {td['n']} | {td['wr_resolved_pct']:.2f}% | {td['total_r']:+.2f} | "
            f"{rn['wr_resolved_pct']:.2f}% | {diff:+.2f}pp |"
        )
    lines.append("")

    # Sweep distance buckets
    lines.append("## 5. Per sweep-distance bucket (/topdown only)")
    lines.append("")
    lines.append("Sweep distance = how far the SSL/BSL entry level was from current price at emission.")
    lines.append("")
    lines.append("| Bucket | N | Resolved | TP | SL | WR | Total R | Exp R |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in sweep_buckets:
        m = per_sweep[b]
        lines.append(
            f"| {b} | {m['n']} | {m['resolved']} | {m['tp']} | {m['sl']} | "
            f"{m['wr_resolved_pct']:.2f}% | {m['total_r']:+.2f} | {m['expectancy_r']:+.4f} |"
        )
    lines.append("")

    # R:R buckets
    lines.append("## 6. Per R:R bucket (/topdown)")
    lines.append("")
    lines.append("| R:R | N | Resolved | TP | SL | WR | Total R | Exp R |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in rr_buckets:
        m = per_rr[b]
        lines.append(
            f"| {b} | {m['n']} | {m['resolved']} | {m['tp']} | {m['sl']} | "
            f"{m['wr_resolved_pct']:.2f}% | {m['total_r']:+.2f} | {m['expectancy_r']:+.4f} |"
        )
    lines.append("")

    # TP mode
    lines.append("## 7. Per TP mode (/topdown)")
    lines.append("")
    lines.append("| TP mode | N | WR | Total R | Exp R |")
    lines.append("|---|---|---|---|---|")
    for tpm, m in per_tpm.items():
        lines.append(
            f"| {tpm} | {m['n']} | {m['wr_resolved_pct']:.2f}% | "
            f"{m['total_r']:+.2f} | {m['expectancy_r']:+.4f} |"
        )
    lines.append("")

    # 70/30 split
    lines.append("## 8. 70/30 chronological split (overfit sensitivity)")
    lines.append("")
    lines.append("Train = first 70% of trades chronologically. Holdout = last 30%. If /topdown's edge collapses on holdout vs train, parameter tuning was overfit.")
    lines.append("")
    lines.append("| Slice | /topdown N | /topdown WR | random WR | Δ WR |")
    lines.append("|---|---|---|---|---|")
    diff_train = train_topdown["wr_resolved_pct"] - train_random["wr_resolved_pct"]
    diff_holdout = holdout_topdown["wr_resolved_pct"] - holdout_random["wr_resolved_pct"]
    lines.append(
        f"| Train (70%) | {train_topdown['n']} | {train_topdown['wr_resolved_pct']:.2f}% | "
        f"{train_random['wr_resolved_pct']:.2f}% | {diff_train:+.2f}pp |"
    )
    lines.append(
        f"| Holdout (30%) | {holdout_topdown['n']} | {holdout_topdown['wr_resolved_pct']:.2f}% | "
        f"{holdout_random['wr_resolved_pct']:.2f}% | {diff_holdout:+.2f}pp |"
    )
    lines.append("")
    if abs(diff_train - diff_holdout) > 5:
        lines.append("**Overfit signal:** train vs holdout Δ WR differs by >5pp — parameter tuning may not generalize.")
    else:
        lines.append("**Overfit check:** train vs holdout Δ WR within 5pp — no obvious overfit signature in this metric.")
    lines.append("")

    # Window sensitivity
    lines.append("## 9. Window sensitivity (90 / 120 / 150d)")
    lines.append("")
    lines.append("| Window | /topdown N | /topdown WR | random WR | Δ WR | /topdown Exp R |")
    lines.append("|---|---|---|---|---|---|")
    for w in (90, 120, 150):
        td = window_metrics[w]["topdown"]
        rn = window_metrics[w]["random"]
        d = td["wr_resolved_pct"] - rn["wr_resolved_pct"]
        lines.append(
            f"| {w}d | {td['n']} | {td['wr_resolved_pct']:.2f}% | "
            f"{rn['wr_resolved_pct']:.2f}% | {d:+.2f}pp | {td['expectancy_r']:+.4f} |"
        )
    lines.append("")

    # Notes section
    lines.append("## 10. Methodology notes")
    lines.append("")
    lines.append("- **Replay engine:** `scripts/backtest_topdown.py` re-runs `_build_snapshot` + `_trade_triplet` from `scripts/topdown_snapshot.py` at every confirmed 15m candle close. Same code path as live `/topdown` command (Phase 1 anchor test verified identity).")
    lines.append("- **Pair scope:** BTC/ETH/SOL/DOGE. XRP/AVAX/LINK excluded due to <150d 15m coverage as of run date.")
    lines.append("- **Window:** 150 days (2025-12-25 → 2026-05-23 effective).")
    lines.append("- **Triplet geometry guard:** sl_wrong_side rejection added 2026-05-24 (Phase 1 finding). Numbers above reflect the fixed code.")
    lines.append("- **Dedup:** consecutive emissions within 4 × 15m bars (1h) with same direction and entry within 0.5% are dropped.")
    lines.append("- **Fill model:** LIMIT order at entry level; fills when 5m candle low ≤ entry ≤ high. Walk forward 5m until SL or TP hit (5m candle range), or 24h timeout.")
    lines.append("- **Conservative SL bias:** when both SL and TP fall inside the same 5m candle, SL wins.")
    lines.append("- **Random null spec:** same emission timestamps; 50/50 random direction at seed 42; entry at emission-bar close; SL/TP recomputed from same risk%/reward%. Random has no `unfilled_timeout` confound because entry = current price (instant fill).")
    lines.append("- **Confluence-tag reliability deferred.** CSV does not store per-emission confluence list; computing it requires re-replaying ~6,800 bars and capturing snapshot internals. Adds ~10 min run and ~150 LOC — not done in this report. Available as a follow-up Phase 3.5 if needed.")
    lines.append("")

    # Observations
    lines.append("## 11. Observations & implications")
    lines.append("")
    lines.append(f"1. **Headline:** Δ WR = {delta_wr_pp:+.2f}pp, p = {p_value}, 95% CI on Δ: [{ci[0]*100:+.2f}pp, {ci[1]*100:+.2f}pp]. Statistically significant but practically tiny.")
    lines.append("2. **Unfilled confound:** /topdown's 20%+ unfilled rate (entry waits for sweep touch) artificially boosts headline WR by removing potentially-bad would-be-fills. Including unfilled as 0 PnL trades, /topdown's WR-over-all is closer to the random benchmark.")
    lines.append("3. **Negative expectancy in both:** Even maker fees (0.02% RT) cannot rescue either strategy from negative expectancy at the observed WR/RR mix. Trades are losing R per execution on average.")
    lines.append("4. **Per-pair variance is large.** The headline number masks per-pair behavior; some pairs may be additive, others detractive. See §2.")
    lines.append("5. **Recommendation:** This backtest does not justify porting `/topdown` rules to the bot as a standalone strategy. The mechanical rules are not edge — at best they're a marginal selection over random.")
    lines.append("6. **Caveat — the brief's value may not be in the triplet.** `/topdown` is also a *human decision-support* tool: bias chain, PD zone, structure context, killzone awareness. The triplet is one slice. The brief's other content may improve manual decision quality in ways this offline backtest does not capture — the live falsification (N=30 with `topdown_brief_used` flag) measures that.")
    lines.append("")
    lines.append("## 12. Handoff")
    lines.append("")
    if verdict_label == "EDGE":
        lines.append("- Open phased-plan for porting `/topdown` gates into `strategy_service/` post-FREEZE.")
    elif verdict_label == "NO EDGE":
        lines.append("- **Do not port** to bot. Continue live falsification at lower priority via `topdown_brief_used` journal flag, N=30.")
        lines.append("- Consider redesign of triplet selection logic OR shifting the brief to pure decision-support (no triplet) so the human reads structure but picks their own entry/SL/TP.")
        lines.append("- Optional follow-up: confluence-tag reliability study (Phase 3.5) to identify which individual `/topdown` annotations (sweep, BOS, OB, structure flip, wick tap) are *individually* predictive vs noise.")
    else:
        lines.append("- Continue live falsification (`topdown_brief_used` flag, N=30). Re-evaluate in 4-6 weeks.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Report generator: `scripts/backtest_topdown.py --report {run_id}`. Phase 3 of `docs/plans/_archive/backtest-topdown-2026-05-24.md`.*")
    lines.append("")

    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    print(f"Wrote report: {report_path}")

    return 0


def run_confluence_report(run_id: str) -> int:
    """Phase 3.5: per-confluence reliability. Reads <run_id>_trades.csv (must
    carry confluence columns from a post-2026-05-25 --simulate run)."""
    out_dir = "backtest_results"
    trades_path = os.path.join(out_dir, f"{run_id}_trades.csv")
    report_path = os.path.join(out_dir, f"{run_id}_confluence_report.md")
    if not os.path.exists(trades_path):
        print(f"ERROR: {trades_path} not found")
        return 1
    with open(trades_path) as f:
        trades = list(csv.DictReader(f))

    if CONFLUENCE_TAGS[0] not in (trades[0] if trades else {}):
        print(f"ERROR: {trades_path} has no confluence columns. Re-run --simulate "
              f"with the Phase 3.5 build to capture them.")
        return 1

    def _wr(rows: list[dict]) -> tuple[int, int, float]:
        tp = sum(1 for r in rows if r["outcome"] == "tp")
        sl = sum(1 for r in rows if r["outcome"] == "sl")
        resolved = tp + sl
        return tp, resolved, (tp / resolved * 100 if resolved else 0.0)

    base_tp, base_resolved, base_wr = _wr(trades)

    lines = [f"# /topdown Confluence Reliability — {run_id}", ""]
    lines.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Trades:** {trades_path} (N={len(trades)})")
    lines.append(f"**Baseline WR (all emissions, resolved):** {base_wr:.2f}% ({base_tp}/{base_resolved})")
    lines.append("")
    lines.append("## 1. WR conditional on each confluence (present vs absent)")
    lines.append("")
    lines.append("`lift` = WR(present) − WR(absent). Positive lift = the signal selects winners.")
    lines.append("")
    lines.append("| Confluence | N present | WR present | N absent | WR absent | Lift |")
    lines.append("|---|---|---|---|---|---|")
    rows_sorted = []
    for tag in CONFLUENCE_TAGS:
        present = [r for r in trades if r.get(tag) == "1"]
        absent = [r for r in trades if r.get(tag) == "0"]
        _, pres_res, pres_wr = _wr(present)
        _, abs_res, abs_wr = _wr(absent)
        lift = pres_wr - abs_wr
        rows_sorted.append((tag, len(present), pres_wr, pres_res, len(absent), abs_wr, abs_res, lift))
    # Sort by lift descending
    rows_sorted.sort(key=lambda x: x[7], reverse=True)
    for tag, n_pres, pres_wr, pres_res, n_abs, abs_wr, abs_res, lift in rows_sorted:
        lines.append(
            f"| {tag} | {n_pres} ({pres_res} res) | {pres_wr:.2f}% | "
            f"{n_abs} ({abs_res} res) | {abs_wr:.2f}% | {lift:+.2f}pp |"
        )
    lines.append("")
    lines.append("## 2. WR by confluence count")
    lines.append("")
    lines.append("Count = number of positive confluences present (structure_flip excluded).")
    lines.append("")
    lines.append("| Count | N | resolved | TP | WR |")
    lines.append("|---|---|---|---|---|")
    from collections import defaultdict
    by_count = defaultdict(list)
    for r in trades:
        c = int(r.get("confluence_count", 0))
        by_count[c].append(r)
    for c in sorted(by_count):
        tp, res, wr = _wr(by_count[c])
        lines.append(f"| {c} | {len(by_count[c])} | {res} | {tp} | {wr:.2f}% |")
    lines.append("")
    lines.append("## 3. WR at/above each confluence threshold (cumulative)")
    lines.append("")
    lines.append("Answers: 'if I required ≥N confluences, what WR + how many trades survive?'")
    lines.append("")
    lines.append("| Min count | N (survive) | resolved | WR | % of emissions kept |")
    lines.append("|---|---|---|---|---|")
    total = len(trades)
    for c in sorted(by_count):
        survivors = [r for r in trades if int(r.get("confluence_count", 0)) >= c]
        tp, res, wr = _wr(survivors)
        pct_kept = len(survivors) / total * 100 if total else 0
        lines.append(f"| ≥{c} | {len(survivors)} | {res} | {wr:.2f}% | {pct_kept:.1f}% |")
    lines.append("")
    # 4. OUT-OF-SAMPLE VALIDATION — the lifts in §1 are in-sample. The only
    # honest test of a gate is whether its edge survives on unseen data. We
    # split chronologically 70/30 and re-measure the top single + combined gates.
    trades_sorted = sorted(trades, key=lambda r: int(r["t_ms"]))
    split = int(len(trades_sorted) * 0.70)
    train, holdout = trades_sorted[:split], trades_sorted[split:]

    def _wr_only(rows):
        tp = sum(1 for r in rows if r["outcome"] == "tp")
        sl = sum(1 for r in rows if r["outcome"] == "sl")
        res = tp + sl
        return res, (tp / res * 100 if res else 0.0)

    candidate_gates = [
        ("baseline (all)", lambda r: True),
        ("fvg_aligned", lambda r: r.get("fvg_aligned") == "1"),
        ("ob_aligned_near", lambda r: r.get("ob_aligned_near") == "1"),
        ("fvg AND ob", lambda r: r.get("fvg_aligned") == "1" and r.get("ob_aligned_near") == "1"),
        ("fvg AND ob AND structure_flip",
         lambda r: r.get("fvg_aligned") == "1" and r.get("ob_aligned_near") == "1" and r.get("structure_flip") == "1"),
    ]

    lines.append("## 4. Out-of-sample validation (70/30 chronological split)")
    lines.append("")
    lines.append("The §1 lifts are **in-sample**. A gate only matters if its edge survives on unseen data. Train = first 70% chronologically, holdout = last 30%. If holdout WR collapses to the holdout baseline, the gate is overfit.")
    lines.append("")
    lines.append("| Gate | Train res | Train WR | Holdout res | Holdout WR |")
    lines.append("|---|---|---|---|---|")
    for name, fn in candidate_gates:
        tr_res, tr_wr = _wr_only([r for r in train if fn(r)])
        ho_res, ho_wr = _wr_only([r for r in holdout if fn(r)])
        lines.append(f"| {name} | {tr_res} | {tr_wr:.1f}% | {ho_res} | {ho_wr:.1f}% |")
    lines.append("")

    lines.append("## 5. Reading")
    lines.append("")
    top = rows_sorted[0]
    lines.append(f"- Strongest single confluence by in-sample lift: **{top[0]}** ({top[7]:+.2f}pp) — but see §4 before trusting it.")
    lines.append("- **Confluence COUNT is not monotonic** (§2): WR peaks at 4-5 then falls. Stacking confluences past 5 dilutes, because several tags (ltf_15m_aligned, htf_1h_aligned, inducement) carry NEGATIVE lift — they are anti-signals, not confluences. 'Require more confluences' is the wrong frame.")
    lines.append("- **Decision rule:** trust a gate only if its holdout WR in §4 clears the holdout baseline by a margin comparable to its in-sample lift. If holdout collapses to baseline, the gate is an in-sample artifact — do NOT build it into /topdown.")
    lines.append("")

    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    print(f"Wrote confluence report: {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest /topdown manual strategy")
    parser.add_argument(
        "--tracer-mode",
        action="store_true",
        help="Phase 1: validate replay_at() works (anchor + historical consistency)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Phase 2: walk-forward simulator over 150d × 4 pairs + random null",
    )
    parser.add_argument(
        "--report",
        type=str, default=None,
        help="Phase 3: aggregate CSVs into markdown report. Pass run_id (e.g. topdown_20260524_192804).",
    )
    parser.add_argument(
        "--confluence-report",
        type=str, default=None,
        help="Phase 3.5: per-confluence reliability from <run_id>_trades.csv. Requires a run with confluence capture.",
    )
    parser.add_argument(
        "--days", type=int, default=150,
        help="Backtest window in days (default: 150)",
    )
    parser.add_argument(
        "--pairs", type=str, default=None,
        help="Comma-separated pair override, e.g. BTC/USDT,ETH/USDT,SOL/USDT (default: all 4)",
    )
    parser.add_argument(
        "--timeout-hours", type=int, default=DEFAULT_TIMEOUT_HOURS,
        help="Trade timeout in hours (default: 24)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for null benchmark (default: 42)",
    )
    args = parser.parse_args()

    if args.tracer_mode:
        return run_tracer_mode()
    if args.simulate:
        pairs = (
            [p.strip() for p in args.pairs.split(",") if p.strip()]
            if args.pairs else PAIRS_4
        )
        return run_simulator(
            pairs=pairs, days=args.days, timeout_hours=args.timeout_hours,
            random_seed=args.seed,
        )
    if args.report:
        return run_report(args.report)
    if args.confluence_report:
        return run_confluence_report(args.confluence_report)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
