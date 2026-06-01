"""Journal v2 Phase 3 — auto-classifier top-down chain pre-fill.

Unit-tests trade_classifier._v2_chain via the public classify() entrypoint, plus
the watcher write path (chain auto_* cols + human-col pre-fill + COALESCE guard).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from strategy_service.trade_classifier import classify, CLASSIFIER_VERSION
from data_service.bybit_watcher import BybitWatcher, _V2_CHAIN_MAP


def _snap(direction="long", **over):
    """Minimal context snapshot with a bullish-aligned long by default."""
    snap = {
        "direction": direction,
        "htf_bias": {
            "bias_daily": "bullish",
            "bias_4h": "bullish",
            "bias_1h": "bullish",
            "aligned_with_trade": True,
        },
        "volume_profile": {"zone": "below_va", "near_hvn": {"distance_pct": 0.2}},
        "smc": {
            "obs_nearest": {"1h": {"in_zone": True, "distance_pct": 0.1, "direction": "bullish"}},
            "fvgs_nearest": {},
            "recent_sweeps": [{"tf": "15m", "touch_count": 3}],
            "recent_breaks": [{"type": "bos", "tf": "1h"}],
        },
        "cvd": {"cvd_1h": 100.0},
        "funding": 0.01,
    }
    snap.update(over)
    return snap


def test_classify_emits_v2_chain_keys():
    out = classify(_snap())
    assert out["auto_classifier_version"] == CLASSIFIER_VERSION == 2
    for auto_col, _ in _V2_CHAIN_MAP:
        assert auto_col in out, f"missing {auto_col}"


def test_bullish_aligned_long_chain():
    out = classify(_snap())
    assert out["auto_htf_bias_daily"] == "bullish"
    assert out["auto_htf_bias_4h"] == "bullish"
    assert out["auto_htf_structure_reason"] == "HH_HL"
    assert out["auto_mtf_1h"] == "confirms"
    assert out["auto_location_pd"] == "discount"          # below_va, long-favorable
    assert out["auto_location_quality"] == "key_level"    # OB in_zone + sweep
    assert out["auto_ltf_trigger"] == "sweep_reclaim"     # sweep beats bos
    assert out["auto_structure_type"] == "continuation"   # bos, aligned, not range
    assert out["auto_conf_htf"] is True
    assert out["auto_conf_location"] is True
    assert out["auto_conf_mtf"] is True
    assert out["auto_conf_trigger"] is True
    assert out["auto_conf_noconflict"] is True


def test_undefined_bias_maps_to_range():
    snap = _snap()
    snap["htf_bias"] = {"bias_daily": "undefined", "bias_4h": "undefined",
                        "bias_1h": "undefined", "aligned_with_trade": None}
    out = classify(snap)
    assert out["auto_htf_bias_4h"] == "range"
    assert out["auto_htf_structure_reason"] == "range_bound"
    assert out["auto_structure_type"] == "range"
    assert out["auto_mtf_1h"] == "neutral"
    assert out["auto_conf_htf"] is False


def test_choch_is_reversal_trigger():
    snap = _snap()
    snap["smc"]["recent_sweeps"] = []          # drop sweep so choch wins precedence
    snap["smc"]["recent_breaks"] = [{"type": "choch", "tf": "1h"}]
    out = classify(snap)
    assert out["auto_ltf_trigger"] == "choch"
    assert out["auto_structure_type"] == "reversal"


def test_cvd_against_breaks_noconflict():
    snap = _snap()
    snap["cvd"] = {"cvd_1h": -100.0}           # selling pressure against a long
    out = classify(snap)
    assert out["auto_conf_noconflict"] is False


def test_short_premium_location_favorable():
    snap = _snap(direction="short")
    snap["htf_bias"] = {"bias_daily": "bearish", "bias_4h": "bearish",
                        "bias_1h": "bearish", "aligned_with_trade": True}
    snap["volume_profile"] = {"zone": "above_va"}      # premium — short-favorable
    snap["smc"]["recent_sweeps"] = []
    snap["smc"]["recent_breaks"] = []
    out = classify(snap)
    assert out["auto_location_pd"] == "premium"
    assert out["auto_conf_location"] is True


# --- watcher write path --------------------------------------------------------

def _bare_watcher():
    return BybitWatcher.__new__(BybitWatcher)


def _conn_ctx(cur):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn


def test_insert_annotation_writes_chain_and_prefills_human():
    from datetime import datetime, timezone
    from data_service.bybit_watcher import PositionKey, PositionState

    key = PositionKey(symbol="ETHUSDT", side="Buy", position_idx=0)
    raw = {"symbol": "ETHUSDT", "side": "Buy", "stopLoss": "0"}
    st = PositionState(key=key, size=1.0, entry_price=3000.0, leverage=10.0,
                       updated_at=datetime.now(tz=timezone.utc), raw=raw)
    auto = classify(_snap())

    w = _bare_watcher()
    cur = MagicMock()
    cur.fetchone.return_value = [7]
    conn = _conn_ctx(cur)
    with patch.object(w, "_conn", return_value=conn), \
            patch.object(w, "_get_equity", return_value=None):
        assert w._insert_annotation(st, {"k": "v"}, auto) == 7

    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    # auto_* refreshed unconditionally; human cols COALESCE'd (never clobber a correction).
    assert "auto_ltf_trigger = EXCLUDED.auto_ltf_trigger" in sql
    assert "ltf_trigger = COALESCE(bybit_trade_annotations.ltf_trigger, EXCLUDED.ltf_trigger)" in sql
    # machine value written once for auto_* and once for the human pre-fill.
    assert params.count("sweep_reclaim") == 2
