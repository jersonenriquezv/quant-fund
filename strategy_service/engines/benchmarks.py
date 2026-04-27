"""
Engine 1 benchmarks — null-hypothesis counterfactuals co-emitted alongside
each Engine 1 detection so edge analysis can compare Engine 1's WR / PF
against deterministic baselines on identical trigger candles.

Two benchmarks ship in v1:

- bench_engine1_random_direction
  sha256-driven coin flip on direction. When the flip says "flip", SL/TP
  are mirrored across the entry so R:R is preserved. Tests whether
  Engine 1's directional choice beats noise.

- bench_engine1_market_now
  Same direction as Engine 1 but entry at current_price (no pullback wait).
  SL/TP keep Engine 1's R:R distances measured from current_price. Tests
  whether the pullback retest entry adds edge over an immediate market
  entry on HTF bias at the same trigger moment.

Both share the trigger candle with Engine 1 — they are emitted as
additional `TradeSetup` objects through `evaluate_all()`, produce their
own `ml_setups` rows under their own `setup_type`, and are quarantined to
BTC + ETH via `SHADOW_PAIR_FILTER`.

Dedup safety: main.py keys on `(pair, direction, setup_type)`. Each
benchmark has its own setup_type, so they never suppress Engine 1 nor
each other.
"""

import hashlib
from typing import Callable

from config.settings import settings
from shared.models import TradeSetup


ENGINE_ID = "engine1_trend_pullback"

BENCH_RANDOM_DIRECTION = "bench_engine1_random_direction"
BENCH_MARKET_NOW = "bench_engine1_market_now"


def _coinflip(pair: str, timestamp: int, experiment_id: str) -> bool:
    """Deterministic coin flip from sha256(pair|ts|engine_id|experiment_id).

    Returns True when the flip says "flip the direction". Stable across
    bot restarts and historical replays so any random-direction row in
    ml_setups can be reproduced exactly from its trigger inputs.
    """
    payload = f"{pair}|{timestamp}|{ENGINE_ID}|{experiment_id}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return (digest[0] & 1) == 1


def _flip(direction: str) -> str:
    return "short" if direction == "long" else "long"


def _mirror(entry: float, level: float) -> float:
    """Reflect `level` across `entry` — preserves distance, swaps side."""
    return 2.0 * entry - level


def make_random_direction_bench(
    setup: TradeSetup, *, experiment_id: str,
) -> TradeSetup:
    """Co-emit a random-direction benchmark for an Engine 1 setup.

    Reuses Engine 1's entry/SL/TP geometry. Direction is chosen by a
    deterministic sha256 coin flip; when it says flip, SL/TP are mirrored
    across the entry to keep R:R intact.
    """
    flip = _coinflip(setup.pair, setup.timestamp, experiment_id)
    if flip:
        new_direction = _flip(setup.direction)
        new_sl = _mirror(setup.entry_price, setup.sl_price)
        new_tp1 = _mirror(setup.entry_price, setup.tp1_price)
        new_tp2 = _mirror(setup.entry_price, setup.tp2_price)
    else:
        new_direction = setup.direction
        new_sl = setup.sl_price
        new_tp1 = setup.tp1_price
        new_tp2 = setup.tp2_price
    return TradeSetup(
        timestamp=setup.timestamp,
        pair=setup.pair,
        direction=new_direction,
        setup_type=BENCH_RANDOM_DIRECTION,
        entry_price=setup.entry_price,
        sl_price=new_sl,
        tp1_price=new_tp1,
        tp2_price=new_tp2,
        confluences=[
            f"bench_origin_{setup.setup_type}",
            f"bench_random_flip_{int(flip)}",
        ],
        htf_bias=setup.htf_bias,
        ob_timeframe=setup.ob_timeframe,
        extra_features={
            "bench_engine1_random_flip": int(flip),
            "bench_engine1_origin_direction": setup.direction,
        },
    )


def make_market_now_bench(
    setup: TradeSetup, *, current_price: float,
) -> TradeSetup | None:
    """Co-emit a market-now benchmark for an Engine 1 setup.

    Direction = Engine 1's direction. Entry = current_price (no pullback
    wait). SL/TP placed at the same R-multiples Engine 1 used, measured
    from current_price.

    Returns None when current_price is non-positive or the source setup
    has a zero SL distance (pathological inputs that would yield a
    degenerate benchmark).
    """
    if current_price <= 0:
        return None
    sl_distance = abs(setup.entry_price - setup.sl_price)
    if sl_distance <= 0:
        return None
    tp1_distance = abs(setup.tp1_price - setup.entry_price)
    tp2_distance = abs(setup.tp2_price - setup.entry_price)
    if setup.direction == "long":
        new_sl = current_price - sl_distance
        new_tp1 = current_price + tp1_distance
        new_tp2 = current_price + tp2_distance
    else:
        new_sl = current_price + sl_distance
        new_tp1 = current_price - tp1_distance
        new_tp2 = current_price - tp2_distance
    entry_offset_pct = abs(current_price - setup.entry_price) / current_price
    return TradeSetup(
        timestamp=setup.timestamp,
        pair=setup.pair,
        direction=setup.direction,
        setup_type=BENCH_MARKET_NOW,
        entry_price=current_price,
        sl_price=new_sl,
        tp1_price=new_tp1,
        tp2_price=new_tp2,
        confluences=[
            f"bench_origin_{setup.setup_type}",
            f"bench_market_now_offset_{entry_offset_pct*100:.2f}pct",
        ],
        htf_bias=setup.htf_bias,
        ob_timeframe=setup.ob_timeframe,
        extra_features={
            "bench_engine1_origin_entry": float(setup.entry_price),
            "bench_engine1_origin_sl": float(setup.sl_price),
            "bench_engine1_market_entry_offset_pct": float(entry_offset_pct),
        },
    )


def emit_engine1_benchmarks(
    engine_setup: TradeSetup,
    *,
    current_price: float,
    on_match: Callable[[TradeSetup], bool],
) -> bool:
    """Co-emit any registered Engine 1 benchmarks via the on_match callback.

    Each benchmark is gated by its presence in `settings.SHADOW_MODE_SETUPS`
    so a benchmark can be retired by config alone. Returns True iff
    `on_match` short-circuited (legacy `evaluate()` mode); always False in
    `evaluate_all()` mode where the callback returns False.
    """
    if BENCH_RANDOM_DIRECTION in settings.SHADOW_MODE_SETUPS:
        bench = make_random_direction_bench(
            engine_setup, experiment_id=settings.EXPERIMENT_ID,
        )
        if on_match(bench):
            return True
    if BENCH_MARKET_NOW in settings.SHADOW_MODE_SETUPS:
        bench = make_market_now_bench(
            engine_setup, current_price=current_price,
        )
        if bench is not None and on_match(bench):
            return True
    return False
