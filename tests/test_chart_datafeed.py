"""Unit tests for the TradingView Datafeed endpoints (dashboard/api/routes/chart.py).

No live DB: a minimal FastAPI app mounts only the chart router and
queries.get_candles_range is monkeypatched. Per feedback_tests_env_coupling,
nothing here depends on the dev .env.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.api import queries
from dashboard.api.routes import chart


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(chart.router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def patch_candles(monkeypatch):
    """Replace the DB range query with an in-memory fixture."""
    calls = {}

    async def fake(pair, timeframe, from_ms, to_ms, limit=5000):
        calls["last"] = (pair, timeframe, from_ms, to_ms, limit)
        rows = calls.get("rows", [])
        # Mimic the real query: only rows inside the window.
        return [r for r in rows if from_ms <= r["timestamp"] <= to_ms][:limit]

    monkeypatch.setattr(queries, "get_candles_range", fake)
    return calls


def _candle(ts_ms, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0):
    return {
        "timestamp": ts_ms, "open": o, "high": h, "low": l,
        "close": c, "volume": v, "volume_quote": v * c,
    }


# --- config / symbols / search -----------------------------------------

def test_config_exposes_only_supported_resolutions(client):
    r = client.get("/api/chart/config")
    assert r.status_code == 200
    body = r.json()
    assert body["supported_resolutions"] == ["5", "15", "60", "240", "D", "W"]
    assert body["supports_search"] is True


def test_resolve_symbol_ok_for_btc(client):
    r = client.get("/api/chart/symbols", params={"symbol": "BTC/USDT"})
    assert r.status_code == 200
    info = r.json()
    assert info["ticker"] == "BTC/USDT"
    assert info["session"] == "24x7"
    assert info["supported_resolutions"] == ["5", "15", "60", "240", "D", "W"]


def test_resolve_symbol_rejects_pair_outside_allowlist(client):
    # SOL has deep-enough history for the bot but is OUT of chart scope.
    r = client.get("/api/chart/symbols", params={"symbol": "SOL/USDT"})
    assert r.status_code == 400


def test_search_filters_to_allowlist(client):
    r = client.get("/api/chart/search", params={"query": "BTC"})
    assert r.status_code == 200
    syms = [m["symbol"] for m in r.json()]
    assert syms == ["BTC/USDT"]


def test_search_empty_query_returns_both(client):
    r = client.get("/api/chart/search", params={"query": ""})
    syms = {m["symbol"] for m in r.json()}
    assert syms == {"BTC/USDT", "ETH/USDT"}


# --- history (getBars) --------------------------------------------------

def test_history_maps_resolution_to_timeframe(client, patch_candles):
    patch_candles["rows"] = [_candle(1_700_000_000_000)]
    client.get("/api/chart/history", params={
        "symbol": "ETH/USDT", "resolution": "240",
        "from": 1_700_000_000, "to": 1_700_100_000,
    })
    pair, tf, from_ms, to_ms, _ = patch_candles["last"]
    assert pair == "ETH/USDT"
    assert tf == "4h"  # "240" -> "4h"
    # UDF seconds converted to ms for the DB.
    assert from_ms == 1_700_000_000_000
    assert to_ms == 1_700_100_000_000


def test_history_returns_udf_columns_in_seconds(client, patch_candles):
    patch_candles["rows"] = [
        _candle(1_700_000_000_000, o=10, h=12, l=9, c=11, v=5),
        _candle(1_700_000_300_000, o=11, h=13, l=10, c=12, v=6),
    ]
    r = client.get("/api/chart/history", params={
        "symbol": "BTC/USDT", "resolution": "5",
        "from": 1_700_000_000, "to": 1_700_001_000,
    })
    body = r.json()
    assert body["s"] == "ok"
    assert body["t"] == [1_700_000_000, 1_700_000_300]  # ms -> seconds
    assert body["o"] == [10.0, 11.0]
    assert body["c"] == [11.0, 12.0]


def test_history_rejects_bad_resolution(client):
    r = client.get("/api/chart/history", params={
        "symbol": "BTC/USDT", "resolution": "1",  # 1m is dead/unsupported
        "from": 1, "to": 2,
    })
    assert r.status_code == 400


def test_history_rejects_pair_outside_allowlist(client):
    r = client.get("/api/chart/history", params={
        "symbol": "DOGE/USDT", "resolution": "5", "from": 1, "to": 2,
    })
    assert r.status_code == 400


def test_history_no_data_reports_nexttime(client, patch_candles):
    # Window is empty, but an earlier bar exists -> nextTime points to it.
    patch_candles["rows"] = [_candle(1_699_000_000_000)]
    r = client.get("/api/chart/history", params={
        "symbol": "BTC/USDT", "resolution": "60",
        "from": 1_700_000_000, "to": 1_700_100_000,
    })
    body = r.json()
    assert body["s"] == "no_data"
    assert body["nextTime"] == 1_699_000_000  # ms -> seconds
