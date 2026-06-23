"""Tests for the shadow dashboard query builders (dashboard/api/queries.py).

No real DB: a fake asyncpg pool records the SQL + bound args so we assert on
the generated query (whitelist correctness, orphan-recency bound, experiment
scope). Settings are patched explicitly — never trust the dev .env.

pytest-asyncio is not installed in this repo, so async coroutines are driven
via asyncio.run() in plain sync test functions.
"""

import asyncio

import pytest

from dashboard.api import database as db
from dashboard.api import queries
from config.settings import settings


class _FakeConn:
    def __init__(self, recorder):
        self._rec = recorder
        self._fetch_rows = []
        self._row = {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "total_pnl_usd": 0.0, "avg_pnl_pct": 0.0,
            "best_trade_pct": 0.0, "worst_trade_pct": 0.0,
            "gross_profit": 0.0, "gross_loss": 0.0,
        }

    async def fetch(self, sql, *args):
        self._rec.append((sql, args))
        return list(self._fetch_rows)

    async def fetchrow(self, sql, *args):
        self._rec.append((sql, args))
        return dict(self._row)


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.calls = []
        self._conn = _FakeConn(self.calls)

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.fixture
def fake_pool(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(db, "pg_pool", pool)
    monkeypatch.setattr(settings, "EXPERIMENT_ID", "test_experiment_xyz")
    return pool


def test_open_trades_bound_recency_and_unresolved(fake_pool):
    asyncio.run(queries.get_shadow_trades(status="open"))
    sql, args = fake_pool.calls[-1]
    assert "outcome_type IS NULL" in sql
    assert "interval '48 hours'" in sql  # orphan-recency bound
    assert "experiment_id = $1" in sql
    assert args[0] == "test_experiment_xyz"  # live experiment scope by default


def test_closed_trades_use_terminal_whitelist(fake_pool):
    asyncio.run(queries.get_shadow_trades(status="closed"))
    sql, args = fake_pool.calls[-1]
    assert "outcome_type IN (" in sql
    for o in queries.SHADOW_TERMINAL_OUTCOMES:
        assert o in args
    # Non-market labels must NOT leak in.
    assert "shadow_dedup" not in args
    assert "risk_rejected" not in args
    assert "shadow_orphaned" not in args


def test_experiment_id_override(fake_pool):
    asyncio.run(queries.get_shadow_trades(status="open", experiment_id="custom_exp"))
    _, args = fake_pool.calls[-1]
    assert args[0] == "custom_exp"


def test_setup_type_filter_appended(fake_pool):
    asyncio.run(queries.get_shadow_trades(status="closed", setup_type="engine1_trend_pullback"))
    sql, args = fake_pool.calls[-1]
    assert "setup_type = $" in sql
    assert "engine1_trend_pullback" in args


def test_stats_terminal_whitelist_and_breakdown(fake_pool):
    out = asyncio.run(queries.get_shadow_stats())
    assert len(fake_pool.calls) == 2  # aggregate + per-setup breakdown
    agg_sql, agg_args = fake_pool.calls[0]
    grp_sql, _ = fake_pool.calls[1]
    assert "outcome_type IN (" in agg_sql
    assert agg_args[0] == "test_experiment_xyz"
    for o in queries.SHADOW_TERMINAL_OUTCOMES:
        assert o in agg_args
    assert "GROUP BY setup_type" in grp_sql
    assert out["experiment_id"] == "test_experiment_xyz"
    assert out["by_setup_type"] == []
    assert out["profit_factor"] == 0.0  # gross_loss == 0 guard


def test_stats_win_rate_and_pf_math(fake_pool):
    fake_pool._conn._row = {
        "total_trades": 10, "winning_trades": 6, "losing_trades": 4,
        "total_pnl_usd": 50.0, "avg_pnl_pct": 0.01,
        "best_trade_pct": 0.05, "worst_trade_pct": -0.03,
        "gross_profit": 120.0, "gross_loss": 70.0,
    }
    out = asyncio.run(queries.get_shadow_stats())
    assert out["win_rate"] == pytest.approx(60.0)
    assert out["profit_factor"] == pytest.approx(120.0 / 70.0)
    assert "gross_profit" not in out  # popped


def _eq_row(ts, pnl, setup="engine1", pair="ETH/USDT"):
    return {"resolved_at": ts, "pnl_usd": pnl, "setup_type": setup, "pair": pair}


def test_equity_curve_whitelist_scope_and_filters(fake_pool):
    asyncio.run(queries.get_shadow_equity_curve())
    sql, args = fake_pool.calls[-1]
    assert "outcome_type IN (" in sql
    assert "resolved_at IS NOT NULL" in sql
    assert "pnl_usd IS NOT NULL" in sql
    assert "ORDER BY resolved_at ASC" in sql
    assert args[0] == "test_experiment_xyz"
    for o in queries.SHADOW_TERMINAL_OUTCOMES:
        assert o in args


def test_equity_curve_running_sum_and_drawdown(fake_pool):
    # +100 → -300 → +50  on a 10000 start: equity 10100, 9800, 9850.
    # Peak 10100, trough 9800 → max DD = 300 (2.97% of peak).
    fake_pool._conn._fetch_rows = [
        _eq_row("2026-06-01T00:00:00", 100.0),
        _eq_row("2026-06-02T00:00:00", -300.0),
        _eq_row("2026-06-03T00:00:00", 50.0),
    ]
    out = asyncio.run(queries.get_shadow_equity_curve(start_balance=10000.0))
    assert out["n"] == 3
    assert out["current_balance"] == pytest.approx(9850.0)
    assert out["total_profit"] == pytest.approx(-150.0)
    assert out["return_pct"] == pytest.approx(-1.5)
    assert out["max_drawdown_usd"] == pytest.approx(300.0)
    assert out["max_drawdown_pct"] == pytest.approx(300.0 / 10100.0 * 100, abs=1e-2)
    assert [p["equity"] for p in out["points"]] == [10100.0, 9800.0, 9850.0]


def test_equity_curve_empty_is_flat(fake_pool):
    out = asyncio.run(queries.get_shadow_equity_curve(start_balance=5000.0))
    assert out["n"] == 0
    assert out["current_balance"] == 5000.0
    assert out["total_profit"] == 0.0
    assert out["max_drawdown_usd"] == 0.0
    assert out["points"] == []
