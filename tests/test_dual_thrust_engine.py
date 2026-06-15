"""Unit tests for the Dual Thrust engine brain (CI-safe — no harness needed).

The authoritative fidelity proof is ``scripts/dual_thrust_parity.py`` (compares
trade-for-trade against the Jesse harness, which lives outside this repo). These
tests guard the pure math + live adapter so a refactor that breaks the brain
fails CI even without the harness.
"""

import math

import numpy as np

from shared.models import Candle
from strategy_service.engines import dual_thrust as DT


def test_params_present_and_verbatim():
    assert set(DT.DUAL_THRUST_PARAMS) == {"6h", "4h"}
    # exact values from candidates.json (optimized-rank1-by-train)
    assert DT.DUAL_THRUST_PARAMS["6h"]["up_length"] == 3
    assert DT.DUAL_THRUST_PARAMS["6h"]["down_length"] == 10
    assert math.isclose(DT.DUAL_THRUST_PARAMS["6h"]["up_coeff"], 0.8910825165430803)
    assert math.isclose(DT.DUAL_THRUST_PARAMS["6h"]["stop_loss_atr_rate"], 1.6452234302490119)
    assert DT.DUAL_THRUST_PARAMS["4h"]["up_length"] == 19
    assert DT.DUAL_THRUST_PARAMS["4h"]["down_length"] == 27


def test_compute_thrusts_known_case():
    hp = {"up_coeff": 0.891, "down_coeff": 0.301}
    up_close = np.array([100.0, 100.0, 101.0])
    up_high = np.array([101.0, 101.0, 102.0])
    up_low = np.array([99.0, 99.0, 100.0])
    dn_close = np.array([100.0, 100.0, 101.0])
    dn_low = np.array([99.0, 98.0, 100.0])
    anchor = 100.0
    up_t, dn_t = DT.compute_thrusts(up_close, up_high, up_low, dn_close, dn_low,
                                    anchor, hp)
    # up: max(close.max-low.min, high.max-close.min) = max(101-99, 102-100)=2
    assert math.isclose(up_t, 100.0 + 0.891 * 2.0)
    # down quirk uses low col: max(dn_close.max-dn_low.min, dn_low.max-dn_close.min)
    #   = max(101-98, 100-100) = 3
    assert math.isclose(dn_t, 100.0 - 0.301 * 3.0)


def test_raw_signal_thresholds():
    assert DT.raw_signal(102.0, 101.0, 95.0) == 1     # above upper -> long
    assert DT.raw_signal(94.0, 101.0, 95.0) == -1     # below lower -> short
    assert DT.raw_signal(98.0, 101.0, 95.0) == 0      # between -> flat
    assert DT.raw_signal(101.0, 101.0, 95.0) == 0     # strict >, equal = flat


def test_wilder_atr_warmup_and_finite():
    n = 40
    high = np.linspace(100, 110, n)
    low = high - 2.0
    close = high - 1.0
    atr = DT.wilder_atr(high, low, close, period=14)
    assert np.isnan(atr[:13]).all()        # warmup region
    assert not math.isnan(atr[13])         # first valid at period-1
    assert (atr[14:] > 0).all()


def test_day_open_map_uses_day_start_open():
    # two UTC days of 6h bars; first bar of each day carries the day open
    day1 = 1_714_435_200_000  # 2024-04-30 00:00 UTC (matches CSV cadence)
    six_h = 6 * 3600 * 1000
    ts, op, hi, lo, cl = [], [], [], [], []
    for d, base in ((0, 1000.0), (1, 2000.0)):
        for k in range(4):
            ts.append(day1 + d * 24 * 3600 * 1000 + k * six_h)
            op.append(base + k)       # first bar (k=0) open == base == day open
            hi.append(base + k + 1)
            lo.append(base + k - 1)
            cl.append(base + k + 0.5)
    dmap = DT.day_open_map(ts, op, hi, lo, cl)
    assert math.isclose(dmap[day1], 1000.0)
    assert math.isclose(dmap[day1 + 24 * 3600 * 1000], 2000.0)


def test_latest_signal_too_short_returns_none():
    hp = DT.DUAL_THRUST_PARAMS["6h"]
    candles = [
        Candle(timestamp=i, open=100, high=101, low=99, close=100,
               volume=1, volume_quote=1, pair="ETH/USDT", timeframe="6h",
               confirmed=True)
        for i in range(5)
    ]
    assert DT.latest_signal(candles, hp) is None


def test_latest_signal_detects_breakout():
    hp = DT.DUAL_THRUST_PARAMS["6h"]
    six_h = 6 * 3600 * 1000
    day0 = 1_714_435_200_000
    candles = []
    # ~3 UTC days of calm 6h bars near 100, then a sharp breakout up
    for k in range(40):
        ts = day0 + k * six_h
        candles.append(Candle(timestamp=ts, open=100.0, high=100.5, low=99.5,
                              close=100.0, volume=1, volume_quote=1,
                              pair="ETH/USDT", timeframe="6h", confirmed=True))
    # final bar: large up-close, clearly above any plausible up_thrust
    ts = day0 + 40 * six_h
    candles.append(Candle(timestamp=ts, open=100.0, high=130.0, low=100.0,
                          close=129.0, volume=1, volume_quote=1,
                          pair="ETH/USDT", timeframe="6h", confirmed=True))
    bar = DT.latest_signal(candles, hp)
    assert bar is not None
    assert bar.sig == 1
    assert bar.up_thrust is not None and bar.price > bar.up_thrust
