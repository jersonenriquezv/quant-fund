"""Engine1 maker fill-rate study.

Replays historical engine1_trend_pullback ETH/USDT short shadow setups against
the persisted 5m candles and estimates the realistic fill rate of a `post_only`
limit at the engine's `entry_price`.

Background: shadow currently fills as soon as a candle wicks to entry_price
(`_candle_touched_price` in shadow_monitor.py:335). On real OKX, a `post_only`
limit only fills if (a) the price reaches the limit AND (b) the order is not
behind a queue too deep to clear. This script models several queue-depth proxies
by requiring the wick to extend past the limit by a margin.

Outputs per scenario:
  posted_rate   = fraction where post_only would have been accepted at placement
  fill_rate     = of posted, fraction where wick reached entry within window
  effective_n   = posted * fill_rate * 37   (number of trades that would have
                  filled live as maker)

Comparison: the existing 37 shadow rows assume 100% fill on touch.

Run: PYTHONPATH=. python scripts/engine1_fillrate_study.py
"""

from __future__ import annotations

import dataclasses
from typing import Sequence

import numpy as np
import psycopg2

from config.settings import settings


SETUP_TYPE = "engine1_trend_pullback"
EXPERIMENT_ID = "redesign_pre_2026_04_27"
PAIR = "ETH/USDT"
DIRECTION = "short"

ENTRY_TIMEOUT_HOURS = settings.SHADOW_ENTRY_TIMEOUT_HOURS
TIMEFRAME = "5m"
TF_MS = 5 * 60 * 1000

# Queue-depth proxies. A wick must extend past entry_price by this fraction of
# entry_price to count as a fill under the given assumption.
MARGIN_SCENARIOS_BPS = (0, 1, 3, 5, 10)


@dataclasses.dataclass
class Setup:
    setup_id: str
    created_at_ms: int
    direction: str
    entry_price: float
    sl_price: float
    tp2_price: float
    outcome_type: str
    pnl_usd: float
    notional: float


def fetch_setups(cur) -> list[Setup]:
    cur.execute(
        """
        SELECT setup_id,
               EXTRACT(EPOCH FROM created_at) * 1000 AS created_at_ms,
               direction,
               entry_price, sl_price, tp2_price,
               outcome_type, pnl_usd,
               shadow_position_size * entry_price AS notional
        FROM ml_setups
        WHERE setup_type = %s
          AND experiment_id = %s
          AND pair = %s
          AND direction = %s
          AND outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven','shadow_timeout')
        ORDER BY created_at
        """,
        (SETUP_TYPE, EXPERIMENT_ID, PAIR, DIRECTION),
    )
    return [
        Setup(
            setup_id=r[0],
            created_at_ms=int(r[1]),
            direction=r[2],
            entry_price=float(r[3]),
            sl_price=float(r[4]),
            tp2_price=float(r[5]),
            outcome_type=r[6],
            pnl_usd=float(r[7]),
            notional=float(r[8]),
        )
        for r in cur.fetchall()
    ]


def fetch_candles(cur, pair: str, ts_from_ms: int, ts_to_ms: int) -> list[tuple]:
    cur.execute(
        """
        SELECT timestamp, open, high, low, close
        FROM candles
        WHERE pair = %s AND timeframe = %s
          AND timestamp BETWEEN %s AND %s
        ORDER BY timestamp ASC
        """,
        (pair, TIMEFRAME, ts_from_ms, ts_to_ms),
    )
    return cur.fetchall()


def candle_at_or_before(cur, pair: str, ts_ms: int) -> tuple | None:
    cur.execute(
        """
        SELECT timestamp, open, high, low, close
        FROM candles
        WHERE pair = %s AND timeframe = %s AND timestamp <= %s
        ORDER BY timestamp DESC LIMIT 1
        """,
        (pair, TIMEFRAME, ts_ms),
    )
    return cur.fetchone()


def post_only_valid_at_placement(direction: str, entry_price: float, ref_price: float) -> bool:
    """Approximate post_only validity check.

    Sell limit (short entry) must be ABOVE the reference price; buy limit
    (long entry) must be BELOW. If the entry has already been crossed at
    placement time, post_only is rejected.
    """
    if direction == "short":
        return entry_price > ref_price
    return entry_price < ref_price


def fill_with_margin(direction: str, entry_price: float, candles: Sequence[tuple], margin_bps: int) -> tuple[bool, int]:
    """Return (filled, n_candles_to_fill).

    Requires the wick to extend past entry_price by `margin_bps`. For a short
    sell limit, that means high must exceed entry_price * (1 + margin_bps/10000).
    For a long buy limit, low must drop below entry_price * (1 - margin_bps/10000).
    """
    margin = margin_bps / 10000.0
    if direction == "short":
        threshold = entry_price * (1.0 + margin)
        for i, (_ts, _o, high, _l, _c) in enumerate(candles):
            if float(high) >= threshold:
                return True, i + 1
    else:
        threshold = entry_price * (1.0 - margin)
        for i, (_ts, _o, _h, low, _c) in enumerate(candles):
            if float(low) <= threshold:
                return True, i + 1
    return False, len(candles)


def main() -> None:
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )
    cur = conn.cursor()

    setups = fetch_setups(cur)
    print(f"Loaded {len(setups)} setups for {SETUP_TYPE} {PAIR} {DIRECTION}")

    # First pass: post_only validity at placement
    posted = []
    rejected = []
    for s in setups:
        ref = candle_at_or_before(cur, PAIR, s.created_at_ms)
        if ref is None:
            rejected.append((s, "no-candle"))
            continue
        ref_close = float(ref[4])
        if post_only_valid_at_placement(s.direction, s.entry_price, ref_close):
            posted.append(s)
        else:
            rejected.append((s, f"crossed: ref_close={ref_close:.2f} entry={s.entry_price:.2f}"))

    posted_rate = len(posted) / len(setups) if setups else 0.0
    print(f"\nPost_only placement validity:")
    print(f"  posted   : {len(posted)} / {len(setups)} = {posted_rate * 100:.1f}%")
    print(f"  rejected : {len(rejected)} (post_only would not have been accepted)")
    if rejected:
        print("  reject reasons (first 5):")
        for s, why in rejected[:5]:
            print(f"    {s.setup_id} {why}")

    # Second pass: fill within entry timeout, varying margin
    window_ms = int(ENTRY_TIMEOUT_HOURS * 3600 * 1000)
    print(f"\nFill rates within {ENTRY_TIMEOUT_HOURS}h entry window (5m candles):")

    by_margin: dict[int, dict] = {}
    for margin_bps in MARGIN_SCENARIOS_BPS:
        fills = 0
        fill_latency_5m = []
        filled_setup_ids: list[str] = []
        for s in posted:
            from_ms = s.created_at_ms
            to_ms = s.created_at_ms + window_ms
            candles = fetch_candles(cur, PAIR, from_ms, to_ms)
            ok, n = fill_with_margin(s.direction, s.entry_price, candles, margin_bps)
            if ok:
                fills += 1
                fill_latency_5m.append(n)
                filled_setup_ids.append(s.setup_id)
        rate = fills / len(posted) if posted else 0.0
        avg_lat = np.mean(fill_latency_5m) if fill_latency_5m else 0.0
        by_margin[margin_bps] = {
            "fills": fills,
            "rate": rate,
            "avg_latency_5m": avg_lat,
            "filled_ids": set(filled_setup_ids),
        }
        print(f"  +{margin_bps:>2}bps  fills={fills:>2}/{len(posted)} = {rate * 100:5.1f}%   avg_latency={avg_lat:5.1f} × 5m bars")

    # Outcome reconstruction: of post_only-fillable setups, compute net PnL
    # under several fee scenarios
    print(f"\nNet PnL projection (assumes outcome unchanged on filled subset):")
    print(f"  Original shadow assumed taker x2 (0.10% RT) -> pnl_usd already net.")
    print(f"  Backing out gross, then re-applying alternative fee models.")

    TAKER_RT = 0.001
    print(f"\n{'margin':<8}{'fills':<8}{'gross_sum':>12}{'tk_net':>10}{'mk-tk_net':>12}{'mk-mk_net':>12}")
    for margin_bps, data in by_margin.items():
        filled_ids = data["filled_ids"]
        sub = [s for s in posted if s.setup_id in filled_ids]
        if not sub:
            print(f"  +{margin_bps}bps  no fills")
            continue
        pnl_taker_net = np.array([s.pnl_usd for s in sub])
        notional = np.array([s.notional for s in sub])
        gross = pnl_taker_net + notional * TAKER_RT
        mk_tk = gross - notional * 0.0007
        mk_mk = gross - notional * 0.0004
        print(f"  +{margin_bps:>2}bps {len(sub):>5} {gross.sum():>+11.2f} {pnl_taker_net.sum():>+9.2f} {mk_tk.sum():>+11.2f} {mk_mk.sum():>+11.2f}")

    # Also report baseline for comparison
    print("\nBaseline (all 37 setups, current shadow assumption):")
    pnl_all = np.array([s.pnl_usd for s in setups])
    notional_all = np.array([s.notional for s in setups])
    gross_all = pnl_all + notional_all * TAKER_RT
    print(f"  gross sum = {gross_all.sum():+.2f}   taker-net sum = {pnl_all.sum():+.2f}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
