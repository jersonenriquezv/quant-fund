"""
Phase 1 of manual-edge-discipline plan — verifies trigger_condition and
thesis_invalidation are wired through:
  - bybit_sync DDL (idempotent ALTER TABLE)
  - Pydantic models (AnnotationUpdate accepts both, AnnotationOut exposes both)
  - row_to_out roundtrip

Plan: docs/plans/manual-edge-discipline-2026-05-15.md
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dashboard.api.routes.bybit import (
    AnnotationOut,
    AnnotationUpdate,
    _row_to_out,
)


def test_annotation_update_accepts_new_fields():
    payload = AnnotationUpdate(
        trigger_condition="rebote en POC 4H 79.2k con vela cuerpo entero + RSI<30 5m",
        thesis_invalidation="cierre 15m > 80.1k = thesis short rota",
    )
    fields = payload.model_dump(exclude_unset=True)
    assert fields["trigger_condition"].startswith("rebote en POC")
    assert "cierre 15m" in fields["thesis_invalidation"]
    # legacy fields still optional
    assert "thesis_pre" not in fields


def test_annotation_update_omits_unset_fields():
    payload = AnnotationUpdate(thesis_pre="only thesis")
    fields = payload.model_dump(exclude_unset=True)
    assert fields == {"thesis_pre": "only thesis"}


def test_row_to_out_roundtrips_new_fields():
    row = {
        "id": 1,
        "symbol": "BTCUSDT",
        "side": "Sell",
        "opened_at": datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        "entry_price": 79200.0,
        "size": 0.05,
        "leverage": 10.0,
        "notional_value": 3960.0,
        "setup_type": "poc_revert",
        "confluences": None,
        "confidence": 3,
        "thesis_pre": "long-form prose",
        "trigger_condition": "rebote en POC 4H 79.2k",
        "thesis_invalidation": "cierre 15m > 80.1k",
        "lesson_post": None,
        "emotional_state": "calm",
        "grade_self": None,
        "screenshot_url": None,
        "context_snapshot": None,
        "auto_setup_type": None,
        "auto_confluences": None,
        "auto_detractors": None,
        "auto_grade": None,
        "auto_classifier_version": None,
        "closed_at": None,
        "exit_price": None,
        "pnl_usd": None,
        "pnl_pct": None,
        "status": "open",
        "annotated_at": None,
    }
    out = _row_to_out(row)
    assert isinstance(out, AnnotationOut)
    assert out.trigger_condition == "rebote en POC 4H 79.2k"
    assert out.thesis_invalidation == "cierre 15m > 80.1k"


def test_row_to_out_handles_null_new_fields():
    row = {
        "id": 2,
        "symbol": "ETHUSDT",
        "side": "Buy",
        "opened_at": datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        "entry_price": None,
        "size": None,
        "leverage": None,
        "notional_value": None,
        "setup_type": None,
        "confluences": None,
        "confidence": None,
        "thesis_pre": None,
        "trigger_condition": None,
        "thesis_invalidation": None,
        "lesson_post": None,
        "emotional_state": None,
        "grade_self": None,
        "screenshot_url": None,
        "context_snapshot": None,
        "auto_setup_type": None,
        "auto_confluences": None,
        "auto_detractors": None,
        "auto_grade": None,
        "auto_classifier_version": None,
        "closed_at": None,
        "exit_price": None,
        "pnl_usd": None,
        "pnl_pct": None,
        "status": "open",
        "annotated_at": None,
    }
    out = _row_to_out(row)
    assert out.trigger_condition is None
    assert out.thesis_invalidation is None


def test_bybit_sync_ddl_declares_new_columns():
    """Sanity check that the migration source includes both ALTER lines.

    Importing BybitSync requires DB env; we read source text instead to keep
    the test free of side effects.
    """
    import importlib.util
    from pathlib import Path

    src = Path("data_service/bybit_sync.py").read_text()
    assert "ADD COLUMN IF NOT EXISTS trigger_condition TEXT" in src
    assert "ADD COLUMN IF NOT EXISTS thesis_invalidation TEXT" in src


# --- Phase 5: journal v2 form fields -----------------------------------------

def test_annotation_update_accepts_v2_chain_and_review():
    payload = AnnotationUpdate(
        htf_bias_daily="bullish",
        htf_bias_4h="range",
        location_pd="discount",
        ltf_trigger="sweep_reclaim",
        structure_type="reversal",
        conf_htf=True,
        conf_trigger=True,
        planned_entry_price=79250.0,
        planned_sl_price=78400.0,
        risk_pct=1.0,
        followed_process=False,
        technical_error=["sl_bad_placement"],
        behavioral_error=["revenge_overtrade", "held_loser"],
    )
    fields = payload.model_dump(exclude_unset=True)
    assert fields["htf_bias_4h"] == "range"
    assert fields["conf_htf"] is True
    assert fields["followed_process"] is False
    assert fields["behavioral_error"] == ["revenge_overtrade", "held_loser"]


def test_annotation_update_rejects_bad_enum():
    with pytest.raises(Exception):
        AnnotationUpdate(htf_bias_4h="sideways")  # not in bullish/bearish/range
    with pytest.raises(Exception):
        AnnotationUpdate(ltf_trigger="vibes")


def test_annotation_update_rejects_unknown_error_tag():
    with pytest.raises(Exception):
        AnnotationUpdate(technical_error=["misread_structure", "made_it_up"])
    with pytest.raises(Exception):
        AnnotationUpdate(behavioral_error=["not_a_real_tag"])


def test_annotation_update_empty_error_array_allowed():
    # '[]' = reviewed-clean (distinct from NULL = not reviewed).
    payload = AnnotationUpdate(technical_error=[], behavioral_error=[])
    fields = payload.model_dump(exclude_unset=True)
    assert fields["technical_error"] == []
    assert fields["behavioral_error"] == []


def test_row_to_out_maps_v2_columns():
    row = {
        "id": 9, "symbol": "ETHUSDT", "side": "Buy",
        "opened_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "status": "closed",
        "journal_schema_version": 2,
        "htf_bias_4h": "bullish",
        "auto_htf_bias_4h": "range",   # machine diverges from human
        "conf_htf": True,
        "tf_aligned_count": 4,
        "planned_entry_price": 3000.0,
        "followed_process": True,
        "technical_error": '["sl_bad_placement"]',  # JSONB arrives as str
        "behavioral_error": [],
        "clean_sample": True,
        "trade_quality": "good_win",
        "realized_r": 1.8,
        "mae_mfe_tf": "1m",
    }
    out = _row_to_out(row)
    assert out.journal_schema_version == 2
    assert out.htf_bias_4h == "bullish"
    assert out.auto_htf_bias_4h == "range"
    assert out.tf_aligned_count == 4
    assert out.technical_error == ["sl_bad_placement"]
    assert out.behavioral_error == []
    assert out.clean_sample is True
    assert out.realized_r == 1.8


def test_jsonb_cols_set_covers_error_arrays():
    from dashboard.api.routes.bybit import _JSONB_COLS
    assert {"confluences", "technical_error", "behavioral_error"} <= _JSONB_COLS
