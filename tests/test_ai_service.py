"""Tests for ai_service.service — full AIService.evaluate() integration."""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from ai_service.service import AIService
from shared.models import TradeSetup, MarketSnapshot
from config.settings import settings


def _make_setup(direction="long", pair="BTC/USDT") -> TradeSetup:
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair,
        direction=direction,
        setup_type="setup_a",
        entry_price=50000.0,
        sl_price=49000.0,
        tp1_price=51000.0,
        tp2_price=52000.0,
        confluences=["choch", "ob", "sweep"],
        htf_bias="bullish",
        ob_timeframe="15m",
    )


def _make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        pair="BTC/USDT",
        timestamp=int(time.time() * 1000),
    )


def _mock_settings():
    """Return a dict of settings attrs for patching."""
    return {
        "ANTHROPIC_API_KEY": "test-key",
        "CLAUDE_MODEL": "claude-sonnet-4-20250514",
        "AI_MIN_CONFIDENCE": 0.60,
        "AI_TIMEOUT_SECONDS": 30.0,
        "AI_MAX_TOKENS": 500,
        "AI_TEMPERATURE": 0.3,
        "FUNDING_EXTREME_THRESHOLD": 0.0003,
    }


def _make_ai_service_with_mock(claude_result: dict | None) -> AIService:
    """Create AIService with mocked ClaudeClient."""
    mock_attrs = _mock_settings()

    with (
        patch("ai_service.service.settings", **mock_attrs),
        patch("ai_service.claude_client.settings", **mock_attrs),
        patch("ai_service.prompt_builder.settings", **mock_attrs),
    ):
        service = AIService(data_service=None)

    # Replace claude client with mock
    service._claude = AsyncMock()
    service._claude.evaluate = AsyncMock(return_value=claude_result)
    return service


# ============================================================
# Approval scenarios
# ============================================================

class TestApproval:

    def test_approve_high_confidence(self):
        service = _make_ai_service_with_mock({
            "confidence": 0.75,
            "approved": True,
            "reasoning": "Strong setup",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is True
        assert decision.confidence == 0.75

    def test_boundary_confidence_060_approved(self):
        service = _make_ai_service_with_mock({
            "confidence": 0.60,
            "approved": True,
            "reasoning": "Marginal but acceptable",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is True

    def test_adjustments_passed_through(self):
        service = _make_ai_service_with_mock({
            "confidence": 0.70,
            "approved": True,
            "reasoning": "Good with tighter SL",
            "adjustments": {"sl_price": 49200.0},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.adjustments == {"sl_price": 49200.0}

    def test_warnings_passed_through(self):
        service = _make_ai_service_with_mock({
            "confidence": 0.65,
            "approved": True,
            "reasoning": "OK but risky",
            "adjustments": {},
            "warnings": ["High funding rate", "Low volume"],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert len(decision.warnings) == 2


# ============================================================
# Rejection scenarios
# ============================================================

class TestRejection:

    def test_reject_low_confidence(self):
        service = _make_ai_service_with_mock({
            "confidence": 0.40,
            "approved": True,  # Claude says yes but confidence too low
            "reasoning": "Weak setup",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is False

    def test_reject_claude_says_no(self):
        service = _make_ai_service_with_mock({
            "confidence": 0.70,
            "approved": False,
            "reasoning": "Macro conditions unfavorable",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is False

    def test_boundary_confidence_059_rejected(self):
        service = _make_ai_service_with_mock({
            "confidence": 0.59,
            "approved": True,
            "reasoning": "Almost there",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is False

    def test_api_failure_rejects(self):
        service = _make_ai_service_with_mock(None)
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is False
        assert "failed" in decision.reasoning.lower()

    def test_no_api_key_rejects_all(self):
        with patch("ai_service.service.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = ""
            service = AIService(data_service=None)

        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is False
        assert "disabled" in decision.reasoning.lower()


# ============================================================
# Confidence clamping
# ============================================================

class TestConfidenceClamping:

    def test_confidence_clamped_above_one(self):
        service = _make_ai_service_with_mock({
            "confidence": 1.5,
            "approved": True,
            "reasoning": "Over-confident",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.confidence == 1.0

    def test_confidence_clamped_below_zero(self):
        service = _make_ai_service_with_mock({
            "confidence": -0.3,
            "approved": False,
            "reasoning": "Negative",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.confidence == 0.0


# ============================================================
# Data service integration
# ============================================================

# ============================================================
# Profile-aware confidence thresholds
# ============================================================

class TestProfileConfidence:

    def test_aggressive_min_confidence_050_approved(self):
        """Aggressive profile approves at confidence 0.50."""
        service = _make_ai_service_with_mock({
            "confidence": 0.50,
            "approved": True,
            "reasoning": "Setup has strong confluence",
            "adjustments": {},
            "warnings": [],
        })
        original = settings.AI_MIN_CONFIDENCE
        try:
            settings.AI_MIN_CONFIDENCE = 0.50  # aggressive threshold
            decision = asyncio.run(
                service.evaluate(_make_setup(direction="short"), _make_snapshot())
            )
            assert decision.approved is True
            assert decision.confidence == 0.50
        finally:
            settings.AI_MIN_CONFIDENCE = original

    def test_default_min_confidence_rejects_050(self):
        """Default profile rejects confidence 0.50 (below 0.60 threshold)."""
        service = _make_ai_service_with_mock({
            "confidence": 0.50,
            "approved": True,
            "reasoning": "Marginal setup",
            "adjustments": {},
            "warnings": [],
        })
        original = settings.AI_MIN_CONFIDENCE
        try:
            settings.AI_MIN_CONFIDENCE = 0.60  # default threshold
            decision = asyncio.run(
                service.evaluate(_make_setup(), _make_snapshot())
            )
            assert decision.approved is False
        finally:
            settings.AI_MIN_CONFIDENCE = original


# ============================================================
# Data service integration
# ============================================================

class TestDataServiceIntegration:

    def test_evaluate_without_data_service(self):
        """data_service=None should work, just no candles context."""
        service = _make_ai_service_with_mock({
            "confidence": 0.70,
            "approved": True,
            "reasoning": "OK",
            "adjustments": {},
            "warnings": [],
        })
        decision = asyncio.run(
            service.evaluate(_make_setup(), _make_snapshot())
        )
        assert decision.approved is True
