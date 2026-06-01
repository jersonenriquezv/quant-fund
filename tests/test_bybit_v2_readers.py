"""Journal v2 Phase 6 — readers + stats migration.

Covers the pure/data-shaping pieces that don't need a live DB:
  - dashboard v2-stats Decimal->float jsonifier + route registration
  - weekly_review_bybit.build_user_prompt emits the v2 chain + R + clean_sample
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal


def test_jsonify_row_converts_decimal():
    from dashboard.api.routes.bybit import _jsonify_row
    out = _jsonify_row({"n": 5, "expectancy_r": Decimal("1.250"), "tag": "held_loser"})
    assert out["n"] == 5
    assert out["expectancy_r"] == 1.25 and isinstance(out["expectancy_r"], float)
    assert out["tag"] == "held_loser"


def test_v2_stats_route_registered_and_v2_filtered():
    from dashboard.api.routes import bybit
    paths = {r.path for r in bybit.router.routes}
    assert "/bybit/v2-stats" in paths
    # every v2 query must wall off legacy v1 rows
    assert "journal_schema_version = 2" in bybit._V2_BASE


def _v2_trade(**over):
    t = {
        "id": 7, "symbol": "BTCUSDT", "side": "Sell",
        "opened_at": datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        "status": "closed", "entry_price": 79250.0, "exit_price": 78400.0,
        "leverage": 10.0, "size": 0.05, "pnl_usd": 42.5, "pnl_pct": 1.07,
        "setup_type": "B_sweep", "thesis_pre": "short into premium",
        "lesson_post": "", "emotional_state": "calm",
        "journal_schema_version": 2,
        "htf_bias_daily": "bearish", "htf_bias_4h": "bearish",
        "location_pd": "premium", "location_quality": "key_level",
        "mtf_1h": "confirms", "ltf_trigger": "sweep_reclaim",
        "structure_type": "reversal", "tf_aligned_count": 4,
        "followed_process": True, "clean_sample": True,
        "technical_error": [], "behavioral_error": [],
        "realized_r": 1.8, "mfe_r": 2.1, "mae_r": -0.4, "exit_efficiency": 0.86,
        "context_snapshot": {"htf_bias": {"bias_4h": "bearish", "aligned_with_trade": True}},
    }
    t.update(over)
    return t


def test_build_user_prompt_emits_v2_chain_and_metrics():
    from scripts.weekly_review_bybit import build_user_prompt
    prompt = build_user_prompt([_v2_trade()], days=7)
    # summary carries the v2 discipline slice
    assert '"v2_closed": 1' in prompt
    assert '"v2_clean": 1' in prompt
    assert '"v2_clean_expectancy_r": 1.8' in prompt
    # per-trade chain + R + process surfaced (not just legacy free text)
    payload = prompt.split("Trades (all 1):\n", 1)[1]
    row = json.loads(payload.split("\n\nWrite", 1)[0])[0]
    assert row["chain"]["ltf_trigger"] == "sweep_reclaim"
    assert row["chain"]["tf_aligned_count"] == 4
    assert row["clean_sample"] is True
    assert row["realized_r"] == 1.8
    assert row["behavioral_error"] == []
    # legacy grade_self / confluences dropped from the row
    assert "grade_self" not in row
    assert "confluences" not in row


def test_build_user_prompt_v1_row_has_no_v2_clean():
    from scripts.weekly_review_bybit import build_user_prompt
    v1 = _v2_trade(journal_schema_version=1, clean_sample=None, realized_r=None)
    prompt = build_user_prompt([v1], days=7)
    assert '"v2_closed": 0' in prompt
    assert '"v2_clean_expectancy_r": null' in prompt
