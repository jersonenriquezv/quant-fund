"""Unit tests for the strict Bybit-trade -> /topdown-alert matcher."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data_service.topdown_reconcile import (
    MATCH_ENTRY_TOL_PCT,
    MATCH_WINDOW_HOURS,
    bybit_symbol_to_pair,
    find_matching_alert,
    side_to_direction,
)

OPEN = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)


class StubCursor:
    """Returns a fixed alert row set; ignores the SQL/params (logic lives in find_matching_alert
    only for the entry-tolerance + first-row pick, the time/pair/dir filter is asserted via the
    rows we feed it)."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params):
        self._last_params = params

    def fetchall(self):
        return self._rows


def _alert(aid, entry, scanned, pair="BTC/USDT", direction="short", sl=100.0, tp=90.0, rr=2.0):
    return (aid, pair, direction, entry, sl, tp, rr, scanned)


def test_symbol_and_side_helpers():
    assert bybit_symbol_to_pair("BTCUSDT") == "BTC/USDT"
    assert bybit_symbol_to_pair("ETHUSDT") == "ETH/USDT"
    assert bybit_symbol_to_pair("BTCUSD") is None
    assert side_to_direction("Buy") == "long"
    assert side_to_direction("Sell") == "short"
    assert side_to_direction("weird") is None


def test_match_within_entry_tolerance():
    # alert entry 100.0; trade entry 100.4 = 0.4% < 0.6% gate -> match
    rows = [_alert(643, 100.0, OPEN - timedelta(hours=3))]
    m = find_matching_alert(
        StubCursor(rows), symbol="BTCUSDT", side="Sell",
        entry_price=100.4, opened_at=OPEN,
    )
    assert m is not None
    assert m.alert_id == 643
    assert m.entry_diff_pct < MATCH_ENTRY_TOL_PCT
    assert abs(m.lead_hours - 3.0) < 0.01


def test_reject_entry_too_far():
    # 2% off -> beyond tolerance -> no match
    rows = [_alert(1, 100.0, OPEN - timedelta(hours=1))]
    m = find_matching_alert(
        StubCursor(rows), symbol="BTCUSDT", side="Sell",
        entry_price=102.0, opened_at=OPEN,
    )
    assert m is None


def test_most_recent_alert_wins():
    # Two candidates in-window; rows arrive newest-first (mirrors SQL ORDER BY scanned_at DESC).
    rows = [
        _alert(2, 100.0, OPEN - timedelta(hours=2)),
        _alert(1, 100.0, OPEN - timedelta(hours=20)),
    ]
    m = find_matching_alert(
        StubCursor(rows), symbol="BTCUSDT", side="Sell",
        entry_price=100.1, opened_at=OPEN,
    )
    assert m.alert_id == 2


def test_unknown_symbol_or_side_returns_none():
    rows = [_alert(1, 100.0, OPEN - timedelta(hours=1))]
    assert find_matching_alert(StubCursor(rows), symbol="BTCUSD", side="Sell",
                               entry_price=100.0, opened_at=OPEN) is None
    assert find_matching_alert(StubCursor(rows), symbol="BTCUSDT", side="x",
                               entry_price=100.0, opened_at=OPEN) is None


def test_naive_opened_at_is_tolerated():
    rows = [_alert(5, 100.0, OPEN - timedelta(hours=1))]
    m = find_matching_alert(
        StubCursor(rows), symbol="BTCUSDT", side="Sell",
        entry_price=100.0, opened_at=OPEN.replace(tzinfo=None),
    )
    assert m is not None and m.alert_id == 5


def test_window_constant_sane():
    assert MATCH_WINDOW_HOURS == 36.0
    assert MATCH_ENTRY_TOL_PCT == 0.6
