"""Dual Thrust engine — signal brain ported VERBATIM from the validated harness.

Source of truth: ``~/jesse-research/project/okx_revalidation.py`` (the OKX
revalidation harness that produced Sharpe ~1.999 / +206% on ETH-USDT-SWAP 6h,
passing walk-forward + Monte Carlo + Binance->OKX transfer).

WHAT IS COPIED VERBATIM (do NOT refactor — this is "the edge"):
  - ``wilder_atr`` (Wilder's ATR, same recursion)
  - the thrust thresholds (anchor +/- coeff * max(range), incl. the documented
    ``down`` low-column quirk)
  - the raw long/short/flat signal (price vs thrusts)
  - the 1D anchor derivation (resample trade-TF bars -> 1D open, no lookahead)

WHAT IS NOT HERE: the execution model (fills, flip lifecycle, sizing, fees).
In the harness that is *simulated* (next-bar-open fills). Live, real orders
replace it (Phase 1, ``campaign_monitor``-style flip engine). The simulated
fills are deliberately excluded so the brain stays isolated and provable.

A parity gate (``scripts/dual_thrust_parity.py``) proves this module reproduces
the harness trade-for-trade before any live capital. If you edit the math here,
parity MUST still pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from shared.models import Candle

# --- Optimized params, VERBATIM from ~/jesse-research/project/candidates.json
#     (source = "optimized-rank1-by-train"). Keyed by trade timeframe. ---
DUAL_THRUST_PARAMS: dict[str, dict] = {
    "6h": {
        "stop_loss_atr_rate": 1.6452234302490119,
        "down_length": 10,
        "up_length": 3,
        "down_coeff": 0.30106933690452525,
        "up_coeff": 0.8910825165430803,
    },
    "4h": {
        "stop_loss_atr_rate": 0.5946796883655397,
        "down_length": 27,
        "up_length": 19,
        "down_coeff": 0.5808587225280869,
        "up_coeff": 0.7109981857884029,
    },
}

ATR_PERIOD = 14  # harness ATR_PERIOD


# ---------------------------------------------------------------------------
# Brain (verbatim math)
# ---------------------------------------------------------------------------
def wilder_atr(high, low, close, period: int = ATR_PERIOD):
    """Wilder's ATR. VERBATIM from the harness — do not change the recursion."""
    high, low, close = map(np.asarray, (high, low, close))
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    atr = np.full_like(tr, np.nan, dtype=float)
    if len(tr) < period:
        return atr
    atr[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def compute_thrusts(up_close, up_high, up_low, dn_close, dn_low,
                    anchor_open: float, hp: dict) -> tuple[float, float]:
    """Upper/lower Dual Thrust thresholds. VERBATIM from harness loop part (c).

    Includes the documented ``down_thrust`` quirk: the second range term uses
    the *low* column (``dn_low.max() - dn_close.min()``) exactly as the source
    rule that was optimized. This quirk is part of the validated result.
    """
    upC, dnC = hp["up_coeff"], hp["down_coeff"]
    up_thrust = anchor_open + upC * max(
        up_close.max() - up_low.min(), up_high.max() - up_close.min())
    down_thrust = anchor_open - dnC * max(
        dn_close.max() - dn_low.min(), dn_low.max() - dn_close.min())  # quirk: low col
    return up_thrust, down_thrust


def raw_signal(price: float, up_thrust: float, down_thrust: float) -> int:
    """+1 long, -1 short, 0 flat. VERBATIM (price strictly beyond the thrust)."""
    long_cond = price > up_thrust
    short_cond = price < down_thrust
    return 1 if long_cond else (-1 if short_cond else 0)


# ---------------------------------------------------------------------------
# Anchor derivation (1D open from trade-TF bars) — matches harness _resample_ohlc
# ---------------------------------------------------------------------------
def day_open_map(timestamps, opens, highs, lows, closes) -> dict[int, float]:
    """Map UTC-day-start ms -> that day's open, by resampling trade-TF bars to 1D.

    Mirrors the harness: for the OKX leg the 1D anchor is built from the trade
    bars themselves (``_resample_ohlc(6h -> 1D)``), label/closed=left, taking the
    first bar's open. No separate 1D feed; no lookahead.
    """
    df = pd.DataFrame({
        "timestamp": np.asarray(timestamps),
        "open": np.asarray(opens, dtype=float),
        "high": np.asarray(highs, dtype=float),
        "low": np.asarray(lows, dtype=float),
        "close": np.asarray(closes, dtype=float),
    })
    idx = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    s = df.set_index(idx)
    agg = s.resample("1D", label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna()
    day_ms = (agg.index.values.astype("datetime64[ns]").astype("int64") // 1_000_000)
    return {int(t): float(o) for t, o in zip(day_ms, agg["open"].to_numpy(float))}


def _bar_day_ms(timestamps) -> np.ndarray:
    """Each bar's UTC-midnight ms (the day it belongs to). Matches harness bar_day."""
    return (pd.to_datetime(np.asarray(timestamps), unit="ms", utc=True)
            .normalize().values.astype("datetime64[ns]").astype("int64") // 1_000_000)


# ---------------------------------------------------------------------------
# Signal replay (batch) — the brain over a full candle history
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DualThrustBar:
    """One evaluated bar. ``sig`` is the position-INDEPENDENT raw signal."""
    timestamp: int
    price: float
    atr: float
    anchor_open: Optional[float]
    up_thrust: Optional[float]
    down_thrust: Optional[float]
    sig: int  # +1 / -1 / 0


def replay_signals(timestamps, opens, highs, lows, closes, hp: dict
                   ) -> list[DualThrustBar]:
    """Evaluate the raw signal for every bar (warmup bars excluded).

    This is the LIVE brain run in batch: each bar's signal depends only on
    closed data up to and including that bar (verbatim harness slicing). The
    same per-bar computation is what the live engine calls on each new
    confirmed candle (see ``latest_signal``).
    """
    timestamps = np.asarray(timestamps)
    o = np.asarray(opens, dtype=float)
    h = np.asarray(highs, dtype=float)
    low = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)

    atr = wilder_atr(h, low, c)
    dmap = day_open_map(timestamps, o, h, low, c)
    bar_day = _bar_day_ms(timestamps)

    upL, dnL = hp["up_length"], hp["down_length"]
    warm = max(upL, dnL, ATR_PERIOD)

    out: list[DualThrustBar] = []
    for i in range(warm, len(c)):
        price = c[i]
        a = atr[i]
        anchor_open = dmap.get(int(bar_day[i]))
        if anchor_open is None or math.isnan(a):
            out.append(DualThrustBar(int(timestamps[i]), float(price), float(a),
                                     anchor_open, None, None, 0))
            continue
        up_close = c[i - upL + 1:i + 1]
        up_high = h[i - upL + 1:i + 1]
        up_low = low[i - upL + 1:i + 1]
        dn_close = c[i - dnL + 1:i + 1]
        dn_low = low[i - dnL + 1:i + 1]
        up_thrust, down_thrust = compute_thrusts(
            up_close, up_high, up_low, dn_close, dn_low, anchor_open, hp)
        sig = raw_signal(price, up_thrust, down_thrust)
        out.append(DualThrustBar(int(timestamps[i]), float(price), float(a),
                                 float(anchor_open), float(up_thrust),
                                 float(down_thrust), sig))
    return out


# ---------------------------------------------------------------------------
# Live adapter (Candle objects)
# ---------------------------------------------------------------------------
def latest_signal(candles: Sequence[Candle], hp: dict) -> Optional[DualThrustBar]:
    """Raw signal for the most recent (closed) candle in ``candles``.

    ``candles`` must be confirmed, chronological, single-pair/single-TF, and
    long enough to cover warmup (``max(up_length, down_length, ATR_PERIOD)`` + a
    full UTC day for the anchor). Returns ``None`` if too short.
    """
    if len(candles) < max(hp["up_length"], hp["down_length"], ATR_PERIOD) + 1:
        return None
    bars = replay_signals(
        [c.timestamp for c in candles],
        [c.open for c in candles],
        [c.high for c in candles],
        [c.low for c in candles],
        [c.close for c in candles],
        hp,
    )
    return bars[-1] if bars else None
