"""Tests for ai_service.claude_client — API wrapper with mocked Anthropic responses."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from ai_service.claude_client import ClaudeClient
from anthropic import APIError, APITimeoutError, RateLimitError


def _make_mock_response(content_text: str):
    """Create a mock Anthropic response object."""
    block = MagicMock()
    block.text = content_text
    response = MagicMock()
    response.content = [block]
    return response


@pytest.fixture
def valid_json():
    return json.dumps({
        "confidence": 0.75,
        "approved": True,
        "reasoning": "Strong setup with HTF confluence",
        "adjustments": {},
        "warnings": [],
    })


# ============================================================
# Constructor
# ============================================================

class TestConstructor:

    @patch("ai_service.claude_client.settings")
    def test_no_api_key_raises(self, mock_settings):
        mock_settings.ANTHROPIC_API_KEY = ""
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            ClaudeClient()


# ============================================================
# Evaluate
# ============================================================

class TestEvaluate:

    @patch("ai_service.claude_client.settings")
    def test_valid_json_parsed(self, mock_settings, valid_json):
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.AI_TIMEOUT_SECONDS = 30.0
        mock_settings.AI_MAX_TOKENS = 500
        mock_settings.AI_TEMPERATURE = 0.3

        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            return_value=_make_mock_response(valid_json)
        )

        result = asyncio.run(client.evaluate("system", "user"))
        assert result is not None
        assert result["confidence"] == 0.75
        assert result["approved"] is True

    @patch("ai_service.claude_client.settings")
    def test_json_with_code_fences_parsed(self, mock_settings, valid_json):
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.AI_TIMEOUT_SECONDS = 30.0
        mock_settings.AI_MAX_TOKENS = 500
        mock_settings.AI_TEMPERATURE = 0.3

        wrapped = f"```json\n{valid_json}\n```"
        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            return_value=_make_mock_response(wrapped)
        )

        result = asyncio.run(client.evaluate("system", "user"))
        assert result is not None
        assert result["confidence"] == 0.75

    @patch("ai_service.claude_client.settings")
    def test_invalid_json_returns_none(self, mock_settings):
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.AI_TIMEOUT_SECONDS = 30.0
        mock_settings.AI_MAX_TOKENS = 500
        mock_settings.AI_TEMPERATURE = 0.3

        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            return_value=_make_mock_response("This is not JSON at all")
        )

        result = asyncio.run(client.evaluate("system", "user"))
        assert result is None

    @patch("ai_service.claude_client.settings")
    def test_missing_fields_returns_none(self, mock_settings):
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.AI_TIMEOUT_SECONDS = 30.0
        mock_settings.AI_MAX_TOKENS = 500
        mock_settings.AI_TEMPERATURE = 0.3

        incomplete = json.dumps({"confidence": 0.5})  # missing approved, reasoning
        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            return_value=_make_mock_response(incomplete)
        )

        result = asyncio.run(client.evaluate("system", "user"))
        assert result is None

    @patch("ai_service.claude_client.settings")
    def test_api_timeout_returns_none(self, mock_settings):
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.AI_TIMEOUT_SECONDS = 30.0
        mock_settings.AI_MAX_TOKENS = 500
        mock_settings.AI_TEMPERATURE = 0.3

        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            side_effect=APITimeoutError(request=MagicMock())
        )

        result = asyncio.run(client.evaluate("system", "user"))
        assert result is None

    @patch("ai_service.claude_client.settings")
    def test_rate_limit_returns_none(self, mock_settings):
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.AI_TIMEOUT_SECONDS = 30.0
        mock_settings.AI_MAX_TOKENS = 500
        mock_settings.AI_TEMPERATURE = 0.3

        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            side_effect=RateLimitError(
                message="rate limited",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        )

        result = asyncio.run(client.evaluate("system", "user"))
        assert result is None

    @patch("ai_service.claude_client.settings")
    def test_api_error_returns_none(self, mock_settings):
        mock_settings.ANTHROPIC_API_KEY = "test-key"
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.AI_TIMEOUT_SECONDS = 30.0
        mock_settings.AI_MAX_TOKENS = 500
        mock_settings.AI_TEMPERATURE = 0.3

        client = ClaudeClient()
        client._client = AsyncMock()
        client._client.messages.create = AsyncMock(
            side_effect=APIError(
                message="server error",
                request=MagicMock(),
                body=None,
            )
        )

        result = asyncio.run(client.evaluate("system", "user"))
        assert result is None
