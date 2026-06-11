"""Historical zone retest rates for the /chart detection overlay.

Answers the trader question: "when a fresh OB/FVG zone appears on the chart,
how often does price come back and touch it?" Measured per (zone type,
timeframe, direction) over BTC/ETH history, using the SAME detectors and the
same 600-bar sliding window as the chart's detection_timeline endpoint — so the
zones counted here are exactly the zones the overlay paints.

Method, per zone lifecycle from the replay:
  1. LEAVE  — find the first bar after birth that is ENTIRELY outside the zone
              (bar.low > zone.high or bar.high < zone.low). OBs/FVGs are born
              with price at/inside the band, so a touch only counts after price
              has clearly left.
  2. TOUCH  — after leaving, any bar overlapping [low, high] = RETEST.
  3. Window — birth .. the zone's real expiry in the replay (mitigation/fill
              kill the zone right after the touch; age kills it untouched).

Buckets:
  retested      — left the zone, came back            -> numerator
  no_retest     — zone expired (age) without a touch  -> denominator only
  never_left    — price never cleanly left before expiry (not a tradeable
                  retest setup) -> EXCLUDED
  censored      — zone still alive at the end of data without a touch
                  (outcome unknown) -> EXCLUDED, prevents downward bias

Output: dashboard/api/data/retest_stats.json, consumed by the
detection_timeline endpoint (zones gain `retest_pct` when N >= MIN_N).

READ-ONLY on the DB. Re-run whenever you want fresher numbers:
    python scripts/chart_retest_stats.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from dashboard.api.routes.chart import _replay_detection_timeline, ZONE_OPEN_TS  # noqa: E402
from shared.models import Candle  # noqa: E402

PAIRS = ["BTC/USDT", "ETH/USDT"]
# Bars per timeframe — full history where cheap, capped where the O(n*window)
# replay would crawl. Overridable via --bars.
TF_BARS = {"1d": 0, "4h": 0, "1h": 0, "15m": 8000, "5m": 10000}
REPLAY_WINDOW = 600  # same cap as the chart endpoint
MIN_N = 30  # below this the JSON still records the bucket but pct is null

OUT_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "api" / "data" / "retest_stats.json"


def fetch_candles(conn, pair: str, timeframe: str, limit: int) -> list[Candle]:
    """Newest `limit` candles (0 = all), returned oldest-first."""
    with conn.cursor() as cur:
        sql = (
            "SELECT timestamp, open, high, low, close, volume, volume_quote "
            "FROM candles WHERE pair=%s AND timeframe=%s ORDER BY timestamp DESC"
        )
        params: tuple = (pair, timeframe)
        if limit:
            sql += " LIMIT %s"
            params += (limit,)
        cur.execute(sql, params)
        rows = cur.fetchall()
    rows.reverse()
    return [
        Candle(
            pair=pair, timeframe=timeframe, timestamp=int(r[0]),
            open=float(r[1]), high=float(r[2]), low=float(r[3]),
            close=float(r[4]), volume=float(r[5]),
            volume_quote=float(r[6] or 0.0), confirmed=True,
        )
        for r in rows
    ]


def replay_zones(candles: list[Candle], pair: str, timeframe: str) -> list[dict]:
    """Zone lifecycles over the full candle range using a sliding 600-bar window.

    The endpoint replays a single 600-bar window; here the history is longer, so
    each bar sees the trailing 600 bars (>> any zone max_age in bars — zones die
    of age long before they'd slide out of the window). Detector instances and
    the timeline accumulator live inside _replay_detection_timeline, which wants
    the whole array — so chunk: replay consecutive 600-bar windows with 300-bar
    overlap and merge by zone key, keeping the first sighting's lifecycle.
    """
    # NOTE: _replay_detection_timeline(candles) is O(n^2) in len(candles); chunking
    # bounds n at 600 while the overlap keeps zones that straddle a boundary.
    step = REPLAY_WINDOW // 2
    seen: dict[str, dict] = {}
    for start in range(0, max(1, len(candles) - REPLAY_WINDOW + step), step):
        chunk = candles[start : start + REPLAY_WINDOW]
        if len(chunk) < 50:
            break
        result = _replay_detection_timeline(chunk, pair, timeframe)
        last_ts = chunk[-1].timestamp
        for z in result["zones"]:
            key = f"{z['type']}:{z['direction']}:{z['timestamp']}:{round(z['high'], 2)}"
            # ZONE_OPEN_TS means "alive at this chunk's end" — for stats, pin it
            # back to the chunk's real last bar so censoring is computed against
            # actual data coverage, not the year-3000 sentinel.
            if z["expire_ts"] == ZONE_OPEN_TS:
                z = {**z, "expire_ts": last_ts, "open_at_end": True}
            else:
                z = {**z, "open_at_end": False}
            prev = seen.get(key)
            if prev is None:
                seen[key] = z
            else:
                # Overlapping chunks see the same zone with partial lifecycles —
                # merge to the true span: earliest birth, latest expiry, and the
                # open-at-end flag from whichever sighting lived longest.
                if z["expire_ts"] >= prev["expire_ts"]:
                    z["born_ts"] = min(z["born_ts"], prev["born_ts"])
                    seen[key] = z
                else:
                    prev["born_ts"] = min(prev["born_ts"], z["born_ts"])
    return list(seen.values())


def classify_zone(zone: dict, candles: list[Candle], last_ts: int) -> str:
    """retested | no_retest | never_left | censored — see module docstring."""
    # OBs: the zone shown on the chart (and traded) is the candle BODY, not the
    # wick-to-wick range — measure retest against the same band the user sees.
    if zone["type"] == "order_block" and zone.get("body_high") is not None:
        lo, hi = zone["body_low"], zone["body_high"]
    else:
        lo, hi = zone["low"], zone["high"]
    born, expire = zone["born_ts"], zone["expire_ts"]
    left = False
    for c in candles:
        if c.timestamp <= born:
            continue
        if c.timestamp > expire:
            break
        if not left:
            if c.low > hi or c.high < lo:
                left = True
            continue
        if c.high >= lo and c.low <= hi:
            return "retested"
    if not left:
        return "never_left"
    # Untouched: resolved only if the zone actually died (age) inside the data.
    if zone.get("open_at_end") and expire >= last_ts:
        return "censored"
    return "no_retest"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", default=",".join(PAIRS))
    ap.add_argument("--bars", default="", help="override TF caps, e.g. 5m=5000,15m=4000")
    args = ap.parse_args()

    tf_bars = dict(TF_BARS)
    if args.bars:
        for part in args.bars.split(","):
            tf, n = part.split("=")
            tf_bars[tf.strip()] = int(n)

    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=10,
    )
    conn.set_session(readonly=True, autocommit=True)

    buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    t0 = time.time()
    for pair in args.pairs.split(","):
        pair = pair.strip()
        for tf, cap in tf_bars.items():
            candles = fetch_candles(conn, pair, tf, cap)
            if len(candles) < 100:
                print(f"{pair} {tf}: only {len(candles)} candles — skipped")
                continue
            zones = replay_zones(candles, pair, tf)
            last_ts = candles[-1].timestamp
            for z in zones:
                outcome = classify_zone(z, candles, last_ts)
                key = f"{z['type']}:{tf}:{z['direction']}"
                buckets[key][outcome] += 1
            print(
                f"{pair} {tf}: {len(candles)} bars, {len(zones)} zones "
                f"({time.time() - t0:.0f}s elapsed)"
            )
    conn.close()

    stats: dict[str, dict] = {}
    for key, b in sorted(buckets.items()):
        n = b["retested"] + b["no_retest"]
        pct = round(100 * b["retested"] / n, 1) if n >= MIN_N else None
        stats[key] = {
            "n": n,
            "retested": b["retested"],
            "pct": pct,
            "excluded_never_left": b["never_left"],
            "excluded_censored": b["censored"],
        }
        shown = f"{pct}%" if pct is not None else f"n/a (N={n}<{MIN_N})"
        print(f"  {key:30s} retest {shown:>14s}  N={n} (+{b['never_left']} never-left, +{b['censored']} censored)")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_at_ms": int(time.time() * 1000),
        "params": {"pairs": args.pairs, "tf_bars": tf_bars, "window": REPLAY_WINDOW, "min_n": MIN_N},
        "stats": stats,
    }, indent=2))
    print(f"\nwrote {OUT_PATH}")


if __name__ == "__main__":
    main()
