"""Unit tests for the detector-replay overlay endpoint (chart.py /detections).

These prove the REPLAY HARNESS (the code in chart.py): detectors are driven
incrementally and expiration is keyed off each bar's own timestamp. The
detector math itself is covered by tests/test_order_block_invariants.py etc.;
true overlay fidelity against a recorded setup is the C3 manual DB gate
(docs/plans/chart-replay-2026-06-01.md), not a unit test.

No live DB: queries.get_candles_range and the detector classes are patched.
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


def _candle_row(ts_ms, c=100.0):
    return {
        "timestamp": ts_ms, "open": c, "high": c + 1, "low": c - 1,
        "close": c, "volume": 10.0, "volume_quote": 10.0 * c,
    }


# --- replay harness contract -------------------------------------------

def test_replay_drives_detectors_incrementally_with_bar_timestamp():
    """Each bar => one OB/FVG update; current_time_ms == that bar's timestamp;
    visible window grows by one; a single detector instance is reused."""
    from shared.models import Candle

    candles = [
        Candle(timestamp=1000 * (i + 1), open=10, high=11, low=9, close=10,
               volume=1, volume_quote=10, pair="BTC/USDT", timeframe="5m",
               confirmed=True)
        for i in range(5)
    ]

    ob_calls, fvg_calls = [], []

    class FakeState:
        structure_breaks = []

    class FakeStructure:
        def analyze(self, visible, pair, tf):
            return FakeState()

    class FakeOB:
        def update(self, visible, breaks, pair, tf, now_ms):
            ob_calls.append((len(visible), now_ms))
            return []

    class FakeFVG:
        def update(self, visible, pair, tf, now_ms):
            fvg_calls.append((len(visible), now_ms))
            return []

    # Patch the classes the module instantiates.
    orig = (chart.MarketStructureAnalyzer, chart.OrderBlockDetector, chart.FVGDetector)
    chart.MarketStructureAnalyzer = FakeStructure
    chart.OrderBlockDetector = FakeOB
    chart.FVGDetector = FakeFVG
    try:
        chart._replay_detections(candles, "BTC/USDT", "5m")
    finally:
        (chart.MarketStructureAnalyzer, chart.OrderBlockDetector,
         chart.FVGDetector) = orig

    # One update per bar, window grows 1..5, time = the bar's own ts.
    assert ob_calls == [(1, 1000), (2, 2000), (3, 3000), (4, 4000), (5, 5000)]
    assert fvg_calls == ob_calls


def test_replay_maps_zone_geometry():
    """Final-bar active zones are serialized with full geometry."""
    from shared.models import Candle

    candles = [
        Candle(timestamp=1000, open=10, high=11, low=9, close=10, volume=1,
               volume_quote=10, pair="BTC/USDT", timeframe="5m", confirmed=True)
    ]

    class FakeState:
        structure_breaks = []

    class FakeStructure:
        def analyze(self, *a):
            return FakeState()

    class FakeOBObj:
        direction = "bullish"; timestamp = 1000; high = 11.0; low = 9.0
        body_high = 10.5; body_low = 9.5; entry_price = 10.0
        mitigated = False; impulse_score = 0.7; retest_count = 2

    class FakeFVGObj:
        direction = "bearish"; timestamp = 1000; high = 12.0; low = 11.0
        size_pct = 0.5; filled_pct = 0.1; fully_filled = False

    class FakeOB:
        def update(self, *a):
            return [FakeOBObj()]

    class FakeFVG:
        def update(self, *a):
            return [FakeFVGObj()]

    orig = (chart.MarketStructureAnalyzer, chart.OrderBlockDetector, chart.FVGDetector)
    chart.MarketStructureAnalyzer = FakeStructure
    chart.OrderBlockDetector = FakeOB
    chart.FVGDetector = FakeFVG
    try:
        out = chart._replay_detections(candles, "BTC/USDT", "5m")
    finally:
        (chart.MarketStructureAnalyzer, chart.OrderBlockDetector,
         chart.FVGDetector) = orig

    assert out["order_blocks"][0] == {
        "type": "order_block", "direction": "bullish", "timestamp": 1000,
        "high": 11.0, "low": 9.0, "body_high": 10.5, "body_low": 9.5,
        "entry_price": 10.0, "mitigated": False, "impulse_score": 0.7,
        "retest_count": 2,
    }
    assert out["fvgs"][0] == {
        "type": "fvg", "direction": "bearish", "timestamp": 1000,
        "high": 12.0, "low": 11.0, "size_pct": 0.5, "filled_pct": 0.1,
        "fully_filled": False,
    }


# --- endpoint ----------------------------------------------------------

def test_detections_rejects_pair_outside_allowlist(client):
    r = client.get("/api/chart/detections", params={
        "symbol": "SOL/USDT", "resolution": "60", "to": 1_700_000_000,
    })
    assert r.status_code == 400


def test_detections_rejects_bad_resolution(client):
    r = client.get("/api/chart/detections", params={
        "symbol": "BTC/USDT", "resolution": "1", "to": 1_700_000_000,
    })
    assert r.status_code == 400


def test_detections_empty_window_returns_empty(client, monkeypatch):
    async def fake(pair, tf, from_ms, to_ms, limit=600):
        return []
    monkeypatch.setattr(queries, "get_candles_range", fake)

    r = client.get("/api/chart/detections", params={
        "symbol": "BTC/USDT", "resolution": "60", "to": 1_700_000_000,
    })
    body = r.json()
    assert body == {"order_blocks": [], "fvgs": [], "as_of": 1_700_000_000, "bars": 0}


def test_detections_runs_real_detectors_and_shapes_response(client, monkeypatch):
    """End-to-end against the REAL detectors on a flat synthetic series:
    no structure break => no zones, but the response envelope is correct and
    the window is fetched as-of `to`."""
    rows = [_candle_row(1_700_000_000_000 + i * 300_000) for i in range(60)]

    captured = {}

    async def fake(pair, tf, from_ms, to_ms, limit=600):
        captured["args"] = (pair, tf, from_ms, to_ms, limit)
        return rows
    monkeypatch.setattr(queries, "get_candles_range", fake)

    r = client.get("/api/chart/detections", params={
        "symbol": "BTC/USDT", "resolution": "5", "to": 1_700_100_000,
    })
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"order_blocks", "fvgs", "as_of", "bars"}
    assert body["bars"] == 60
    assert body["as_of"] == 1_700_100_000
    # Window fetched up to `to` in ms, capped at DETECTION_WINDOW_BARS.
    assert captured["args"] == ("BTC/USDT", "5m", 0, 1_700_100_000_000, 600)


# --- detection timeline (perf: one replay, client-side as-of filtering) ----

def test_timeline_tracks_zone_lifecycle():
    """A zone seen on bars 1-3 (mitigated on bar 3) then gone yields
    born_ts=first sighting, expire_ts=last sighting, spent_ts=first spent bar."""
    from shared.models import Candle

    candles = [
        Candle(timestamp=1000 * (i + 1), open=10, high=11, low=9, close=10,
               volume=1, volume_quote=10, pair="BTC/USDT", timeframe="5m",
               confirmed=True)
        for i in range(5)
    ]

    class FakeState:
        structure_breaks = []

    class FakeStructure:
        def analyze(self, *a):
            return FakeState()

    class FakeOBObj:
        def __init__(self, mitigated):
            self.direction = "bullish"; self.timestamp = 1000; self.high = 11.0
            self.low = 9.0; self.body_high = 10.5; self.body_low = 9.5
            self.entry_price = 10.0; self.mitigated = mitigated
            self.impulse_score = 0.7; self.retest_count = 0

    calls = {"n": 0}

    class FakeOB:
        def update(self, *a):
            calls["n"] += 1
            n = calls["n"]
            if n <= 2:
                return [FakeOBObj(mitigated=False)]
            if n == 3:
                return [FakeOBObj(mitigated=True)]  # mitigated on bar 3
            return []  # gone on bars 4, 5

    class FakeFVG:
        def update(self, *a):
            return []

    orig = (chart.MarketStructureAnalyzer, chart.OrderBlockDetector, chart.FVGDetector)
    chart.MarketStructureAnalyzer = FakeStructure
    chart.OrderBlockDetector = FakeOB
    chart.FVGDetector = FakeFVG
    try:
        out = chart._replay_detection_timeline(candles, "BTC/USDT", "5m")
    finally:
        (chart.MarketStructureAnalyzer, chart.OrderBlockDetector,
         chart.FVGDetector) = orig

    assert len(out["zones"]) == 1
    z = out["zones"][0]
    assert z["type"] == "order_block"
    assert z["born_ts"] == 1000
    assert z["expire_ts"] == 3000
    assert z["spent_ts"] == 3000


def test_timeline_endpoint_shape(client, monkeypatch):
    rows = [_candle_row(1_700_000_000_000 + i * 300_000) for i in range(60)]
    captured = {}

    async def fake(pair, tf, from_ms, to_ms, limit=600):
        captured["args"] = (pair, tf, from_ms, to_ms, limit)
        return rows
    monkeypatch.setattr(queries, "get_candles_range", fake)

    r = client.get("/api/chart/detection_timeline", params={
        "symbol": "BTC/USDT", "resolution": "5", "to": 1_700_100_000,
    })
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"zones", "as_of", "bars"}
    assert body["bars"] == 60
    assert body["as_of"] == 1_700_100_000
    assert isinstance(body["zones"], list)
    assert captured["args"] == ("BTC/USDT", "5m", 0, 1_700_100_000_000, 600)


def test_timeline_empty_window_returns_empty(client, monkeypatch):
    async def fake(pair, tf, from_ms, to_ms, limit=600):
        return []
    monkeypatch.setattr(queries, "get_candles_range", fake)

    r = client.get("/api/chart/detection_timeline", params={
        "symbol": "BTC/USDT", "resolution": "60", "to": 1_700_000_000,
    })
    assert r.json() == {"zones": [], "as_of": 1_700_000_000, "bars": 0}
