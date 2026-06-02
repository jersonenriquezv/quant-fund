"""Unit tests for the signal_scanner edge engine (gate + formatter).

Covers the pure logic only — no DB, no Telegram, no snapshot build:
- `_edge_candidate` gate: sweep cap, geometry guard, rr>0, single-TP passthrough.
- pair scope: SCANNER_PAIRS is BTC/ETH only (edge confirmed nowhere else).
- `_format_telegram_edge`: LIMIT wording + entry price + maker-limit instruction.

See docs/plans/_archive/signal-scanner-topdown-edge-2026-05-25.md (Phase 3).
"""
from __future__ import annotations

from scripts.signal_scanner import (
    MAX_SWEEP_PCT,
    SCANNER_PAIRS,
    _edge_candidate,
    _format_telegram_edge,
)


def _signal(**over):
    """A valid short edge signal; override fields per test."""
    base = {
        "pair": "BTC/USDT",
        "side": "short",
        "entry": 76500.0,
        "sl": 77800.0,       # short → SL above entry (protective)
        "tp": 73600.0,       # single final target
        "rr": 2.18,
        "sweep_distance_pct": 0.17,
        "risk_pct": 1.76,
        "bias_confidence": "medium",
        "current_price": 76400.0,
    }
    base.update(over)
    return base


# --- gate: sweep distance ----------------------------------------------------

def test_sweep_within_cap_accepted():
    cand = _edge_candidate("BTC/USDT", _signal(sweep_distance_pct=MAX_SWEEP_PCT))
    assert cand is not None
    assert cand["sweep_distance_pct"] == MAX_SWEEP_PCT


def test_sweep_over_cap_rejected():
    assert _edge_candidate("BTC/USDT", _signal(sweep_distance_pct=0.51)) is None


def test_sweep_none_rejected():
    assert _edge_candidate("BTC/USDT", _signal(sweep_distance_pct=None)) is None


# --- gate: geometry guard ----------------------------------------------------

def test_short_sl_below_entry_rejected():
    # short with SL <= entry is self-exiting, not a stop
    assert _edge_candidate("BTC/USDT", _signal(side="short", sl=76000.0)) is None


def test_long_sl_above_entry_rejected():
    sig = _signal(side="long", entry=2000.0, sl=2100.0, tp=2300.0)
    assert _edge_candidate("ETH/USDT", sig) is None


def test_long_valid_geometry_accepted():
    sig = _signal(side="long", entry=2000.0, sl=1950.0, tp=2150.0)
    cand = _edge_candidate("ETH/USDT", sig)
    assert cand is not None
    assert cand["sl"] < cand["entry"] < cand["tp"]


# --- gate: rr ----------------------------------------------------------------

def test_rr_zero_rejected():
    assert _edge_candidate("BTC/USDT", _signal(rr=0.0)) is None


def test_rr_negative_rejected():
    assert _edge_candidate("BTC/USDT", _signal(rr=-1.0)) is None


# --- single TP passthrough (scaled never reaches the alert) ------------------

def test_single_tp_is_triplet_final_target():
    cand = _edge_candidate("BTC/USDT", _signal(tp=73600.0))
    assert cand["tp"] == 73600.0
    assert "tp1" not in cand and "tp2" not in cand  # no scaled legs surface


# --- pair scope --------------------------------------------------------------

def test_scanner_pairs_btc_eth_only():
    assert SCANNER_PAIRS == ["BTC/USDT", "ETH/USDT"]


# --- formatter ---------------------------------------------------------------

def test_format_contains_limit_and_price():
    cand = _edge_candidate("BTC/USDT", _signal())
    msg = _format_telegram_edge(cand)
    assert "LIMIT SHORT" in msg
    assert "76500" in msg                 # entry price present
    assert "orden l" in msg.lower()       # maker-limit instruction (orden límite)
    assert "Not executed" in msg


def test_format_single_tp_line():
    cand = _edge_candidate("ETH/USDT", _signal(side="long", entry=2000.0, sl=1950.0, tp=2150.0))
    msg = _format_telegram_edge(cand)
    # exactly one TP line, no scaled TP1/TP2
    assert msg.count(">TP<") == 1
    assert "TP1" not in msg and "TP2" not in msg
