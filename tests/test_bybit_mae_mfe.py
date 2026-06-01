"""Journal v2 Phase 4 — MAE/MFE + R-metric backfill.

Unit-tests the pure excursion/R math and the paginated 1m kline fetch against a
mock client. No DB, no network.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from scripts.compute_bybit_mae_mfe import (
    TradeRow, compute_metrics, fetch_1m_candles, _KLINE_LIMIT,
)


def _row(side="Buy", entry=100.0, sl=90.0, size=2.0, pnl=20.0,
         planned_entry=None, planned_sl=None, position_sl=None):
    return TradeRow(
        annot_id=1, symbol="ETHUSDT", side=side,
        entry_price=entry, size=size, closed_pnl=pnl,
        position_sl=position_sl if position_sl is not None else sl,
        planned_entry=planned_entry, planned_sl=planned_sl,
        opened_ms=1_000_000, closed_ms=2_000_000,
    )


# candles as (high, low)
def test_long_excursion_and_r_metrics():
    # entry 100, sl 90 -> R_price 10, R_usd 20. price ranges high 115, low 95.
    candles = [(105, 98), (115, 95), (108, 102)]
    m = compute_metrics(_row(side="Buy", entry=100, sl=90, size=2.0, pnl=20.0), candles)
    assert m is not None
    assert m.mfe_r == 1.5      # (115-100)/10
    assert m.mae_r == -0.5     # -(100-95)/10, stored <= 0
    assert m.realized_r == 1.0  # 20 / (10*2)
    assert m.exit_efficiency == round(1.0 / 1.5, 4)


def test_short_excursion_flips_sign():
    # short entry 100, sl 110 -> R_price 10. low 92 favorable, high 104 adverse.
    candles = [(104, 92), (101, 95)]
    m = compute_metrics(_row(side="Sell", entry=100, sl=110, size=1.0, pnl=8.0), candles)
    assert m.mfe_r == 0.8      # (100-92)/10
    assert m.mae_r == -0.4     # -(104-100)/10
    assert m.realized_r == 0.8  # 8 / (10*1)


def test_no_adverse_excursion_clamps_to_zero():
    # long, price never dips below entry -> mae_r 0
    candles = [(112, 101), (110, 100)]
    m = compute_metrics(_row(side="Buy", entry=100, sl=90), candles)
    assert m.mae_r == 0.0
    assert m.mfe_r == 1.2


def test_planned_levels_preferred_over_actual():
    # planned R = |102 - 92| = 10; actual entry 100 would give different R.
    candles = [(120, 95)]
    row = _row(side="Buy", entry=100, sl=80, planned_entry=102, planned_sl=92, size=1.0, pnl=10.0)
    m = compute_metrics(row, candles)
    assert m.mfe_r == 1.8       # (120-102)/10 — anchored on planned entry
    assert m.realized_r == 1.0  # 10 / (10*1)


def test_entry_slippage_bps_adverse_positive():
    # long filled at 100 vs planned 99 -> paid 1 more -> adverse +101 bps
    candles = [(110, 95)]
    row = _row(side="Buy", entry=100, sl=90, planned_entry=99, planned_sl=89, size=1.0)
    m = compute_metrics(row, candles)
    assert m.entry_slippage_bps == round((100 - 99) / 99 * 10_000, 2)
    assert m.entry_slippage_bps > 0


def test_slippage_none_without_planned_entry():
    m = compute_metrics(_row(side="Buy"), [(110, 95)])
    assert m.entry_slippage_bps is None


def test_exit_efficiency_none_when_mfe_zero():
    # long, price never exceeds entry -> mfe 0 -> exit_eff None
    candles = [(99, 95), (98, 90)]
    m = compute_metrics(_row(side="Buy", entry=100, sl=90, pnl=-10.0), candles)
    assert m.mfe_r == 0.0
    assert m.exit_efficiency is None


def test_no_r_source_returns_none():
    # no sl anywhere
    row = _row(side="Buy", entry=100, sl=None, position_sl=None)
    assert compute_metrics(row, [(110, 95)]) is None
    # no candles
    assert compute_metrics(_row(), []) is None
    # entry == sl
    assert compute_metrics(_row(entry=100, sl=100, position_sl=100), [(110, 95)]) is None


def test_realized_r_none_without_pnl():
    row = _row(side="Buy", pnl=None)
    m = compute_metrics(row, [(110, 95)])
    assert m.realized_r is None
    assert m.exit_efficiency is None


# --- pagination ---------------------------------------------------------------

def test_fetch_1m_single_page():
    client = MagicMock()
    client.get_kline.return_value = {"result": {"list": [
        ["1000060000", "101", "105", "99", "104", "1", "1"],
        ["1000000000", "100", "103", "98", "101", "1", "1"],
    ]}}
    out = fetch_1m_candles(client, "ETHUSDT", 1_000_000_000, 1_000_120_000)
    assert (105.0, 99.0) in out and (103.0, 98.0) in out
    assert client.get_kline.call_count == 1  # < limit -> stops


def test_fetch_1m_paginates_until_start():
    # First page full (len == limit) and oldest still > start -> must page again.
    page1 = [["%d" % (2_000_000 + i * 60_000), "1", "9", "2", "5", "1", "1"]
             for i in range(_KLINE_LIMIT)][::-1]  # newest-first
    page2 = [["1000000", "1", "8", "3", "5", "1", "1"]]
    client = MagicMock()
    client.get_kline.side_effect = [
        {"result": {"list": page1}},
        {"result": {"list": page2}},
    ]
    out = fetch_1m_candles(client, "ETHUSDT", 1_000_000, 2_000_000 + _KLINE_LIMIT * 60_000)
    assert client.get_kline.call_count == 2
    assert (8.0, 3.0) in out  # from page2


def test_fetch_1m_empty():
    client = MagicMock()
    client.get_kline.return_value = {"result": {"list": []}}
    assert fetch_1m_candles(client, "ETHUSDT", 1, 2) == []
