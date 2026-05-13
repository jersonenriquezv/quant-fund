"""
Rule 6 enforcement — auto-cancel pending limit orders without thesis_pre.

Validates `BybitWatcher._enforce_journal_deadline` behavior for soft-launch
forcing function (see docs/plans/bybit-journal-enforcement.md Phase 2).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from config.settings import settings
from data_service.bybit_watcher import BybitWatcher, PendingOrder


# ================================================================
# Fakes
# ================================================================


class FakeBybitClient:
    def __init__(self, cancel_should_fail: bool = False):
        self.cancel_calls: list[dict] = []
        self.cancel_should_fail = cancel_should_fail

    def cancel_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        if self.cancel_should_fail:
            raise RuntimeError("simulated cancel failure")
        return {"result": {"orderId": kwargs.get("orderId")}}


class FakeCursor:
    def __init__(self, conn: "FakeConn", row_factory: bool = False):
        self.conn = conn
        self.row_factory = row_factory
        self._result: list[Any] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        sql_norm = " ".join(sql.split()).lower()
        if "select order_id, symbol, side, order_type, placed_at" in sql_norm:
            # Enforcement query — return pre-staged candidates
            self._result = list(self.conn.candidates)
        elif sql_norm.startswith("update bybit_pending_orders set enforcement_cancelled_at"):
            order_id = params[0]
            self.conn.cancelled_stamps.append(order_id)
        elif sql_norm.startswith("update bybit_pending_orders"):
            pass  # other updates no-op
        else:
            self._result = []

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, candidates: list[dict] | None = None):
        self.candidates = candidates or []
        self.cancelled_stamps: list[str] = []
        self.committed = False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self, row_factory=cursor_factory is not None)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ================================================================
# Helpers
# ================================================================


def _make_watcher(monkeypatch, candidates: list[dict], enabled: bool = True,
                  deadline_sec: int = 300, cancel_fails: bool = False) -> tuple[BybitWatcher, FakeBybitClient, FakeConn, list[str]]:
    """Build a watcher with all I/O stubbed out."""
    # Bypass __init__ side-effects (real Bybit client, env checks)
    w = BybitWatcher.__new__(BybitWatcher)
    fake_client = FakeBybitClient(cancel_should_fail=cancel_fails)
    w.client = fake_client
    w._dashboard_base = "http://test"
    w._last_state = {}
    w._last_pending = {}

    fake_conn = FakeConn(candidates=candidates)
    monkeypatch.setattr(w, "_conn", lambda: fake_conn)

    monkeypatch.setattr(settings, "BYBIT_JOURNAL_ENFORCEMENT_ENABLED", enabled)
    monkeypatch.setattr(settings, "BYBIT_JOURNAL_ENFORCEMENT_DEADLINE_SEC", deadline_sec)
    monkeypatch.setattr(settings, "BYBIT_JOURNAL_ENFORCEMENT_WHITELIST_ORDER_TYPES", ["Market"])

    telegram_calls: list[str] = []

    async def fake_telegram(text: str) -> None:
        telegram_calls.append(text)

    monkeypatch.setattr(w, "_send_telegram", fake_telegram)
    return w, fake_client, fake_conn, telegram_calls


def _pending(order_id: str, symbol: str = "ETHUSDT", side: str = "Buy",
             order_type: str = "Limit") -> PendingOrder:
    return PendingOrder(
        order_id=order_id, order_link_id="", symbol=symbol, side=side,
        order_type=order_type, qty=0.1, price=2300.0, trigger_price=None,
        stop_order_type="", time_in_force="GTC", reduce_only=False,
        position_idx=0, placed_at=datetime.now(tz=timezone.utc) - timedelta(minutes=10),
    )


def _candidate_row(order_id: str, order_type: str = "Limit",
                   age_minutes: int = 10) -> dict:
    return {
        "order_id": order_id,
        "symbol": "ETHUSDT",
        "side": "Buy",
        "order_type": order_type,
        "placed_at": datetime.now(tz=timezone.utc) - timedelta(minutes=age_minutes),
    }


# ================================================================
# Tests
# ================================================================


def test_enforcement_disabled_skips_cancel(monkeypatch):
    """When BYBIT_JOURNAL_ENFORCEMENT_ENABLED=false, no cancel attempted."""
    candidates = [_candidate_row("OID-1")]
    pending = {"OID-1": _pending("OID-1")}
    w, client, conn, telegram = _make_watcher(monkeypatch, candidates, enabled=False)

    asyncio.run(w._enforce_journal_deadline(pending))

    assert client.cancel_calls == []
    assert conn.cancelled_stamps == []
    assert telegram == []


def test_enforcement_cancels_overdue_limit_without_thesis(monkeypatch):
    """Limit order past deadline with thesis_pre NULL → cancel + telegram + stamp."""
    candidates = [_candidate_row("OID-1", order_type="Limit", age_minutes=10)]
    pending = {"OID-1": _pending("OID-1")}
    w, client, conn, telegram = _make_watcher(monkeypatch, candidates, enabled=True)

    asyncio.run(w._enforce_journal_deadline(pending))

    assert len(client.cancel_calls) == 1
    assert client.cancel_calls[0]["orderId"] == "OID-1"
    assert client.cancel_calls[0]["symbol"] == "ETHUSDT"
    assert client.cancel_calls[0]["category"] == "linear"
    assert "OID-1" in conn.cancelled_stamps
    assert len(telegram) == 1
    assert "AUTO-CANCELADA" in telegram[0]
    assert "ETHUSDT" in telegram[0]


def test_market_order_whitelisted_not_cancelled(monkeypatch):
    """Market orders (whitelist) should not be cancelled even if past deadline."""
    candidates = [_candidate_row("OID-MKT", order_type="Market")]
    pending = {"OID-MKT": _pending("OID-MKT", order_type="Market")}
    w, client, conn, telegram = _make_watcher(monkeypatch, candidates, enabled=True)

    asyncio.run(w._enforce_journal_deadline(pending))

    assert client.cancel_calls == []
    assert conn.cancelled_stamps == []


def test_order_no_longer_pending_skipped(monkeypatch):
    """If candidate is in DB but not in current_pending (already gone), skip — let diff handle."""
    candidates = [_candidate_row("OID-GONE")]
    pending: dict = {}  # not in current Bybit state
    w, client, conn, telegram = _make_watcher(monkeypatch, candidates, enabled=True)

    asyncio.run(w._enforce_journal_deadline(pending))

    assert client.cancel_calls == []
    assert conn.cancelled_stamps == []


def test_cancel_api_failure_alerts_user_no_stamp(monkeypatch):
    """If Bybit cancel_order fails, telegram error sent and stamp NOT applied (will retry next tick)."""
    candidates = [_candidate_row("OID-FAIL")]
    pending = {"OID-FAIL": _pending("OID-FAIL")}
    w, client, conn, telegram = _make_watcher(
        monkeypatch, candidates, enabled=True, cancel_fails=True
    )

    asyncio.run(w._enforce_journal_deadline(pending))

    assert len(client.cancel_calls) == 1
    assert conn.cancelled_stamps == []
    assert len(telegram) == 1
    assert "FAILED" in telegram[0]


def test_no_candidates_no_action(monkeypatch):
    """Empty DB query result → no work done."""
    w, client, conn, telegram = _make_watcher(monkeypatch, candidates=[], enabled=True)

    asyncio.run(w._enforce_journal_deadline({"OID-1": _pending("OID-1")}))

    assert client.cancel_calls == []
    assert telegram == []
