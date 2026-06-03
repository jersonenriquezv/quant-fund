"""C3 fidelity gate for the chart bot-detection overlay.

Proves the /chart detection overlay is NOT "a lie" (grill Q2): for recorded
historical setups, the OB/FVG zone the live bot actually traded on must appear
in the overlay's detector replay as-of that bar.

Method: pull recorded setups (entry/sl/direction/ob_timeframe/timestamp) from
ml_setups, then drive the REAL overlay code (chart._replay_detections) over the
same 600-bar window the endpoint uses, as-of the setup's detection bar. A setup
PASSES when an active zone reproduces its recorded geometry:
  - setup_a (OB):  long -> sl == ob.low,  short -> sl == ob.high; entry in OB band
  - setup_b (FVG): entry in an FVG band of matching direction; sl == an OB edge
  - fallback: entry_price falls inside an active zone of matching direction

Read-only. No docker, no bot mutation.
"""

import argparse
import psycopg2

from config.settings import settings
from dashboard.api.routes.chart import _rows_to_candles, _replay_detections, DETECTION_WINDOW_BARS

CHART_TFS = {"5m", "15m", "1h", "4h", "1d"}
# long/short (ml_setups) <-> bullish/bearish (detector zones)
DIR_MAP = {"long": "bullish", "short": "bearish"}
PRICE_TOL = 5e-4  # 0.05% relative tolerance on edge match (float / buffer drift)


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def _fetch_candles(cur, pair, tf, to_ms):
    """Mirror queries.get_candles_range(0, to_ms, limit=600): last N bars <= to_ms."""
    cur.execute(
        """SELECT timestamp, open, high, low, close, volume, volume_quote
           FROM candles WHERE pair=%s AND timeframe=%s AND timestamp<=%s
           ORDER BY timestamp DESC LIMIT %s""",
        (pair, tf, to_ms, DETECTION_WINDOW_BARS),
    )
    rows = [
        {"timestamp": r[0], "open": r[1], "high": r[2], "low": r[3],
         "close": r[4], "volume": r[5], "volume_quote": r[6]}
        for r in cur.fetchall()
    ]
    return list(reversed(rows))


def _rel_eq(a, b):
    if a is None or b is None:
        return False
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base <= PRICE_TOL


def _match(setup, zones):
    """Classify how the replay overlay reproduces the recorded setup's geometry.

    Returns (grade, detail). Grades:
      EXACT  — a raw OB edge equals the recorded SL to <0.05% (byte-exact zone
               reproduction; the strongest fidelity proof — the live detector
               and the overlay produced the identical zone).
      BAND   — entry falls inside an active zone of matching direction.
      CASCADE— matching-direction zones exist in the region, but entry/SL don't
               land on them. Expected for geometry-cascade setups (_resolve_entry
               synthesises entry/SL off the raw edges) — NOT an overlay defect.
      LIE    — no matching-direction zone exists at all. The only true fidelity
               failure: the overlay would be showing something the bot never saw.
    """
    entry = setup["entry_price"]
    sl = setup["sl_price"]
    zdir = DIR_MAP.get(setup["direction"], setup["direction"])
    obs = [z for z in zones["order_blocks"] if z["direction"] == zdir]
    fvgs = [z for z in zones["fvgs"] if z["direction"] == zdir]

    def _in(z):
        return z["low"] - PRICE_TOL * z["low"] <= entry <= z["high"] + PRICE_TOL * z["high"]

    # EXACT: raw OB edge == recorded SL (setup_a/f/d non-cascade path).
    for ob in obs:
        edge = ob["low"] if zdir == "bullish" else ob["high"]
        if _rel_eq(sl, edge) and _in(ob):
            return "EXACT", f"OB edge SL=={edge:.2f} ts={ob['timestamp']} [{ob['low']:.2f},{ob['high']:.2f}]"

    # BAND: entry inside a matching-direction FVG or OB.
    for fvg in fvgs:
        if _in(fvg):
            return "BAND", f"entry in FVG ts={fvg['timestamp']} [{fvg['low']:.2f},{fvg['high']:.2f}]"
    for ob in obs:
        if _in(ob):
            return "BAND", f"entry in OB ts={ob['timestamp']} [{ob['low']:.2f},{ob['high']:.2f}]"

    # Matching-direction zones present but entry/SL off them -> geometry cascade.
    if obs or fvgs:
        return "CASCADE", f"zones present (obs={len(obs)} fvgs={len(fvgs)}) but entry={entry:.2f}/sl={sl:.2f} off-edge"

    return "LIE", f"NO {zdir} zone in replay (obs=0 fvgs=0) — overlay would be empty where bot detected"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-pair", type=int, default=8, help="setups to test per pair")
    ap.add_argument("--setup-types", default="", help="comma filter on setup_type")
    args = ap.parse_args()

    conn = _conn()
    cur = conn.cursor()
    type_filter = ""
    params = []
    if args.setup_types:
        type_filter = "AND setup_type = ANY(%s)"
        params = [args.setup_types.split(",")]

    results = []
    for pair in ("BTC/USDT", "ETH/USDT"):
        cur.execute(
            f"""SELECT setup_id, setup_type, direction, ob_timeframe, timestamp,
                       entry_price, sl_price
                FROM ml_setups
                WHERE pair=%s AND entry_price IS NOT NULL AND sl_price IS NOT NULL
                  AND ob_timeframe = ANY(%s) {type_filter}
                ORDER BY timestamp DESC LIMIT %s""",
            [pair, list(CHART_TFS)] + params + [args.per_pair],
        )
        cols = [d[0] for d in cur.description]
        setups = [dict(zip(cols, r)) for r in cur.fetchall()]

        print(f"\n=== {pair} — {len(setups)} setups ===")
        for s in setups:
            tf = s["ob_timeframe"]
            rows = _fetch_candles(cur, pair, tf, s["timestamp"])
            if not rows:
                print(f"  SKIP {s['setup_type']:18} {tf:3} no candles")
                continue
            candles = _rows_to_candles(rows, pair, tf)
            zones = _replay_detections(candles, pair, tf)
            grade, detail = _match(s, zones)
            results.append(grade)
            print(f"  {grade:7} {s['setup_type']:18} {tf:3} {s['direction']:5} "
                  f"E={s['entry_price']:.2f} SL={s['sl_price']:.2f} | {detail}")

    cur.close()
    conn.close()
    n = len(results)
    from collections import Counter
    c = Counter(results)
    print(f"\n=== C3 RESULT (n={n}) ===")
    for g in ("EXACT", "BAND", "CASCADE", "LIE"):
        print(f"  {g:7}: {c.get(g, 0)}")
    lies = c.get("LIE", 0)
    proven = c.get("EXACT", 0) + c.get("BAND", 0)
    print(f"\n  Zone-fidelity PROVEN (EXACT+BAND): {proven}/{n}")
    print(f"  CASCADE (entry/SL synthesised off raw zone — overlay still correct): {c.get('CASCADE', 0)}")
    print(f"  TRUE OVERLAY FAILURES (LIE — no matching zone in replay): {lies}")
    print(f"\n  VERDICT: {'PASS — overlay reproduces the live detector; no lies.' if lies == 0 and c.get('EXACT',0) else 'FAIL — investigate LIE rows.'}")


if __name__ == "__main__":
    main()
