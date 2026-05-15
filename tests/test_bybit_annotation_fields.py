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
