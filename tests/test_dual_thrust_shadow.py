"""Tests for the Dual Thrust shadow tracker (order-free flip state machine).

The fill loop is a verbatim port of the harness; here we drive it with a
scripted signal stream (monkeypatching the brain) so the entry/flip/stop state
machine is tested deterministically without network or the ~/jesse-research
harness. End-to-end parity vs the harness lives in scripts/dual_thrust_parity.py.
"""

from __future__ import annotations

import pytest

from execution_service import dual_thrust_shadow as DTS
from strategy_service.engines.dual_thrust import DualThrustBar
from shared.models import Candle

HP = {"up_length": 1, "down_length": 1, "stop_loss_atr_rate": 1.0,
      "up_coeff": 0.5, "down_coeff": 0.5}  # warm = max(1,1,14) = 14
ATR = 10.0


def _candles(n=20, low_dip: dict | None = None):
    """n 4h candles, open=close=100+i, tight range. low_dip overrides low[i]."""
    low_dip = low_dip or {}
    out = []
    for i in range(n):
        base = 100 + i
        out.append(Candle(
            timestamp=1000 + i * 14_400_000, open=float(base),
            high=float(base + 1), low=float(low_dip.get(i, base - 1)),
            close=float(base), volume=1.0, volume_quote=100.0,
            pair="ETH/USDT", timeframe="4h", confirmed=True))
    return out


def _patch_signals(monkeypatch, sig_by_index: dict):
    """Make DT.replay_signals return scripted signals keyed by bar index."""
    def fake(ts, o, h, low, c, hp):
        bars = []
        for i, t in enumerate(ts):
            bars.append(DualThrustBar(
                timestamp=int(t), price=float(c[i]), atr=ATR,
                anchor_open=100.0, up_thrust=float(c[i]) + 1,
                down_thrust=float(c[i]) - 1, sig=sig_by_index.get(i, 0)))
        return bars
    monkeypatch.setattr(DTS.DT, "replay_signals", fake)


def test_flip_long_to_short(monkeypatch):
    # i14 enter long -> i15 fill long@115 -> i16 opposite -> i17 flip exit@117,
    # enter short -> i18 fill short@118.
    _patch_signals(monkeypatch, {14: 1, 15: 1, 16: -1, 17: -1, 18: -1, 19: -1})
    st = DTS.simulate_fills(_candles(), HP)

    assert len(st.trades) == 1
    tr = st.trades[0]
    assert tr.side == 1 and tr.reason == "flip"
    assert tr.entry == 115.0 and tr.exit == 117.0
    assert st.position_side == -1  # now short
    assert st.position_entry == 118.0


def test_stop_loss_hit(monkeypatch):
    # Long fills @115 (stop = 115 - 10 = 105); bar 16 low dips to 100 -> SL.
    _patch_signals(monkeypatch, {14: 1, 15: 1})
    st = DTS.simulate_fills(_candles(low_dip={16: 100.0}), HP)

    assert len(st.trades) == 1
    tr = st.trades[0]
    assert tr.side == 1 and tr.reason == "sl"
    assert tr.exit == 105.0  # filled at the stop
    assert st.position_side == 0  # flat after SL


def test_no_signal_no_trade(monkeypatch):
    _patch_signals(monkeypatch, {})  # all flat
    st = DTS.simulate_fills(_candles(), HP)
    assert st.trades == ()
    assert st.position_side == 0


def test_tracker_filters_pair_and_tf(monkeypatch):
    _patch_signals(monkeypatch, {})
    tracker = DTS.DualThrustShadowTracker(candle_fetcher=_candles)
    btc = Candle(timestamp=1, open=1, high=1, low=1, close=1, volume=1,
                 volume_quote=1, pair="BTC/USDT", timeframe="4h", confirmed=True)
    eth_15m = Candle(timestamp=1, open=1, high=1, low=1, close=1, volume=1,
                     volume_quote=1, pair="ETH/USDT", timeframe="15m", confirmed=True)
    assert tracker.on_candle(btc) is None
    assert tracker.on_candle(eth_15m) is None


def test_tracker_dedup_same_bar(monkeypatch):
    # Tracker uses the real DT params (warm = max(19,27,14) = 27), so signals
    # must sit at indices >= 27 and the window must exceed warmup.
    _patch_signals(monkeypatch, {27: 1, 28: 1, 29: -1, 30: -1, 31: -1})
    candles = _candles(n=32)
    tracker = DTS.DualThrustShadowTracker(candle_fetcher=lambda: candles)
    trigger = candles[-1]  # ETH/USDT 4h

    ev1 = tracker.on_candle(trigger)
    assert ev1 is not None and len(ev1.new_trades) >= 1  # a flip happened
    count_after_first = tracker._last_trade_count
    # Same last_ts -> dedup: returns eval with NO new trades, count unchanged.
    ev2 = tracker.on_candle(trigger)
    assert ev2 is not None and ev2.new_trades == ()
    assert tracker._last_trade_count == count_after_first


def test_tracker_insufficient_candles(monkeypatch):
    _patch_signals(monkeypatch, {})
    tracker = DTS.DualThrustShadowTracker(candle_fetcher=lambda: _candles(n=5))
    trigger = Candle(timestamp=1, open=1, high=1, low=1, close=1, volume=1,
                     volume_quote=1, pair="ETH/USDT", timeframe="4h", confirmed=True)
    assert tracker.on_candle(trigger) is None


def test_format_telegram():
    flip = DTS.ShadowTrade(entry_ts=1, exit_ts=2, side=1, entry=1800.0,
                           exit=1850.0, qty=1.0, pnl_net=50.0, reason="flip")
    msg = DTS.format_telegram(flip, "ETH/USDT")
    assert "FLIP" in msg and "ETH/USDT" in msg
    assert "1,800.00" in msg and "1,850.00" in msg and "+50.00" in msg
    sl = DTS.ShadowTrade(entry_ts=1, exit_ts=2, side=-1, entry=1800.0,
                         exit=1820.0, qty=1.0, pnl_net=-20.0, reason="sl")
    smsg = DTS.format_telegram(sl, "ETH/USDT")
    assert "SL" in smsg and "SHORT" in smsg and "-20.00" in smsg
