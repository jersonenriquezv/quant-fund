"""Journal v2 Phase 2 — watcher data-source capture (SL, equity, 1D bias).

Builds a BybitWatcher without __init__ (which needs API keys + DB), then unit-tests
the new methods against a mocked client + DB cursor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from data_service.bybit_watcher import BybitWatcher, PositionKey, PositionState


def _bare_watcher() -> BybitWatcher:
    return BybitWatcher.__new__(BybitWatcher)


def _conn_ctx(cur):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn


def _state(symbol="BTCUSDT", side="Buy", stop_loss="59000") -> PositionState:
    key = PositionKey(symbol=symbol, side=side, position_idx=0)
    raw = {"symbol": symbol, "side": side, "avgPrice": "60000", "size": "0.1",
           "leverage": "10", "stopLoss": stop_loss}
    return PositionState(key=key, size=0.1, entry_price=60000.0, leverage=10.0,
                         updated_at=datetime.now(tz=timezone.utc), raw=raw)


def test_get_equity_parses_total_equity():
    w = _bare_watcher()
    w.client = MagicMock()
    w.client.get_wallet_balance.return_value = {"result": {"list": [{"totalEquity": "4600.5"}]}}
    assert w._get_equity() == 4600.5


def test_get_equity_none_on_failure():
    w = _bare_watcher()
    w.client = MagicMock()
    w.client.get_wallet_balance.side_effect = RuntimeError("api down")
    assert w._get_equity() is None  # never raises — open must not be blocked


def test_insert_annotation_persists_sl_equity_and_v2():
    w = _bare_watcher()
    cur = MagicMock()
    cur.fetchone.return_value = [123]
    conn = _conn_ctx(cur)
    with patch.object(w, "_conn", return_value=conn), \
            patch.object(w, "_get_equity", return_value=4600.0):
        annot_id = w._insert_annotation(_state(stop_loss="59000"), {"k": "v"},
                                        {"auto_setup_type": "trend_pullback"})
    assert annot_id == 123
    params = cur.execute.call_args[0][1]
    assert 59000.0 in params   # position_sl_price
    assert 4600.0 in params    # account_equity_at_open
    assert 2 in params         # journal_schema_version


def test_insert_annotation_null_sl_when_absent():
    w = _bare_watcher()
    cur = MagicMock()
    cur.fetchone.return_value = [1]
    conn = _conn_ctx(cur)
    with patch.object(w, "_conn", return_value=conn), \
            patch.object(w, "_get_equity", return_value=None):
        w._insert_annotation(_state(stop_loss="0"), {}, {})  # Bybit "0" == no stop
    params = cur.execute.call_args[0][1]
    assert None in params  # sl_price None passes through


def test_refresh_sl_updates_open_row():
    w = _bare_watcher()
    cur = MagicMock()
    conn = _conn_ctx(cur)
    with patch.object(w, "_conn", return_value=conn):
        w._refresh_sl(_state(stop_loss="58000"))
    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "UPDATE bybit_trade_annotations" in sql
    assert "position_sl_price" in sql
    assert 58000.0 in params


def test_refresh_sl_noop_when_no_stop():
    w = _bare_watcher()
    cur = MagicMock()
    conn = _conn_ctx(cur)
    with patch.object(w, "_conn", return_value=conn):
        w._refresh_sl(_state(stop_loss="0"))
    cur.execute.assert_not_called()


def test_backfill_daily_candles_maps_and_upserts():
    w = _bare_watcher()
    w.client = MagicMock()
    w.client.get_kline.return_value = {"result": {"list": [
        ["1717027200000", "60000", "61000", "59500", "60500", "1000", "60250000"],
    ]}}
    cur = MagicMock()
    conn = _conn_ctx(cur)
    import data_service.bybit_watcher as mod
    with patch.object(mod, "settings") as msettings, \
            patch.object(w, "_conn", return_value=conn), \
            patch.object(mod, "execute_batch") as eb:
        msettings.BYBIT_DAILY_BIAS_SYMBOLS = ["BTCUSDT"]
        w._backfill_daily_candles()
    assert eb.called
    rows = eb.call_args[0][2]
    assert rows[0][0] == "BTC/USDT"
    assert rows[0][1] == "1d"
    assert rows[0][2] == 1717027200000
    assert rows[0][6] == 60500.0  # close


def test_backfill_daily_candles_skips_on_empty():
    w = _bare_watcher()
    w.client = MagicMock()
    w.client.get_kline.return_value = {"result": {"list": []}}
    import data_service.bybit_watcher as mod
    with patch.object(mod, "settings") as msettings, \
            patch.object(mod, "execute_batch") as eb:
        msettings.BYBIT_DAILY_BIAS_SYMBOLS = ["BTCUSDT"]
        w._backfill_daily_candles()
    eb.assert_not_called()
