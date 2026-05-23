"""
PnL aggregation across partial closes — `BybitWatcher._close_annotation`.

Bybit emits one closed_pnl row per limit fill that reduces the position.
The watcher must sum every row between annotation.opened_at and now,
not pick the most recent. See bybit_watcher.py:_close_annotation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from data_service.bybit_watcher import BybitWatcher, PositionKey


# ================================================================
# Fakes
# ================================================================


class FakeCursor:
    """Routes the two SELECTs in _close_annotation to pre-staged rows."""

    def __init__(self, conn: "FakeConn"):
        self.conn = conn
        self._result: list[Any] | Any = []
        self._fetchone_target: dict | None = None
        self._update_returning: dict | None = None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        sql_norm = " ".join(sql.split()).lower()
        if "from bybit_trade_annotations" in sql_norm and "where symbol" in sql_norm and "status = 'open'" in sql_norm:
            self._fetchone_target = self.conn.annotation
        elif "from bybit_closed_pnl" in sql_norm and "sum(closed_pnl)" in sql_norm:
            self._fetchone_target = self.conn.pnl_aggregate
        elif sql_norm.startswith("update bybit_trade_annotations"):
            self.conn.update_params = params
            self._fetchone_target = self.conn.update_returning
        else:
            self._fetchone_target = None

    def fetchone(self):
        return self._fetchone_target

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, annotation, pnl_aggregate, update_returning):
        self.annotation = annotation
        self.pnl_aggregate = pnl_aggregate
        self.update_returning = update_returning
        self.update_params: tuple | None = None
        self.committed = False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_watcher(monkeypatch, conn: FakeConn) -> BybitWatcher:
    w = BybitWatcher.__new__(BybitWatcher)
    w.client = None
    w._dashboard_base = "http://test"
    w._last_state = {}
    w._last_pending = {}
    monkeypatch.setattr(w, "_conn", lambda: conn)
    return w


# ================================================================
# Tests
# ================================================================


def test_close_annotation_sums_multiple_partial_fills(monkeypatch):
    """Position closed via 3 limit reduces → annotation gets sum of all closed_pnl rows."""
    opened_at = datetime.now(tz=timezone.utc) - timedelta(hours=48)
    annotation = {
        "id": 42,
        "entry_price": 2300.0,
        "size": 0.30,
        "opened_at": opened_at,
    }
    # 3 partials: $10 + $15 + $5 = $30 PnL on $690 entry value → 4.348%
    # Weighted exit: (0.1*2400 + 0.1*2350 + 0.1*2380) / 0.3 = 2376.67
    pnl_aggregate = {
        "total_pnl": 30.0,
        "total_entry_value": 690.0,
        "total_exit_value": 713.0,
        "weighted_exit_price": 2376.6666667,
        "last_updated": datetime.now(tz=timezone.utc),
        "rows_counted": 3,
        "last_id": 999,
    }
    update_returning = {
        "id": 42,
        "entry_price": 2300.0,
        "size": 0.30,
        "opened_at": opened_at,
    }
    conn = FakeConn(annotation, pnl_aggregate, update_returning)
    w = _make_watcher(monkeypatch, conn)

    result = w._close_annotation(PositionKey(symbol="ETHUSDT", side="Buy"))

    assert result is not None
    assert result["pnl_usd"] == 30.0
    assert result["partial_count"] == 3
    assert result["exit_price"] == pytest.approx(2376.6666667, rel=1e-6)
    assert result["pnl_pct"] == pytest.approx(100.0 * 30.0 / 690.0, rel=1e-6)
    # UPDATE used the aggregated values
    exit_price_param, pnl_param, pnl_pct_param, last_id_param, annot_id_param = conn.update_params
    assert pnl_param == 30.0
    assert last_id_param == 999
    assert annot_id_param == 42
    assert conn.committed


def test_close_annotation_single_fill_behaves_like_legacy(monkeypatch):
    """Single closed_pnl row (full market close) → behaves identically to old single-row path."""
    opened_at = datetime.now(tz=timezone.utc) - timedelta(minutes=20)
    annotation = {
        "id": 7,
        "entry_price": 60000.0,
        "size": 0.01,
        "opened_at": opened_at,
    }
    pnl_aggregate = {
        "total_pnl": -5.0,
        "total_entry_value": 600.0,
        "total_exit_value": 595.0,
        "weighted_exit_price": 59500.0,
        "last_updated": datetime.now(tz=timezone.utc),
        "rows_counted": 1,
        "last_id": 17,
    }
    update_returning = {
        "id": 7,
        "entry_price": 60000.0,
        "size": 0.01,
        "opened_at": opened_at,
    }
    conn = FakeConn(annotation, pnl_aggregate, update_returning)
    w = _make_watcher(monkeypatch, conn)

    result = w._close_annotation(PositionKey(symbol="BTCUSDT", side="Sell"))

    assert result["pnl_usd"] == -5.0
    assert result["partial_count"] == 1
    assert result["exit_price"] == 59500.0
    assert result["pnl_pct"] == pytest.approx(-5.0 / 600.0 * 100.0, rel=1e-6)


def test_close_annotation_no_pnl_rows_still_closes(monkeypatch):
    """Position disappeared but no closed_pnl rows synced yet → annotation still closed with NULL PnL."""
    opened_at = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    annotation = {
        "id": 1,
        "entry_price": 100.0,
        "size": 1.0,
        "opened_at": opened_at,
    }
    pnl_aggregate = {
        "total_pnl": None,
        "total_entry_value": None,
        "total_exit_value": None,
        "weighted_exit_price": None,
        "last_updated": None,
        "rows_counted": 0,
        "last_id": None,
    }
    update_returning = {
        "id": 1,
        "entry_price": 100.0,
        "size": 1.0,
        "opened_at": opened_at,
    }
    conn = FakeConn(annotation, pnl_aggregate, update_returning)
    w = _make_watcher(monkeypatch, conn)

    result = w._close_annotation(PositionKey(symbol="SOLUSDT", side="Buy"))

    assert result is not None
    assert result["pnl_usd"] is None
    assert result["partial_count"] == 0
    assert result["exit_price"] is None
    assert result["pnl_pct"] is None
    # UPDATE still ran with NULL values, last_id=None
    exit_price_param, pnl_param, pnl_pct_param, last_id_param, annot_id_param = conn.update_params
    assert pnl_param is None
    assert last_id_param is None


def test_close_annotation_no_open_annotation_returns_none(monkeypatch):
    """No matching open annotation → return None, skip everything."""
    conn = FakeConn(annotation=None, pnl_aggregate=None, update_returning=None)
    w = _make_watcher(monkeypatch, conn)

    result = w._close_annotation(PositionKey(symbol="XRPUSDT", side="Buy"))

    assert result is None
    assert conn.update_params is None
