"""
Tests for data_service.news_client — Fear & Greed + headlines fetching.

Covers:
- Fear & Greed parsing (valid, error, timeout)
- Headlines parsing (valid, 403, empty)
- NewsSentiment assembly
- Pre-filter integration with sentiment data
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data_service.news_client import NewsClient, _parse_timestamp
from shared.models import NewsHeadline, NewsSentiment
from config.settings import settings


# ================================================================
# Fixtures
# ================================================================

class FakeRedis:
    """Fake RedisStore that uses a dict."""
    def __init__(self):
        self._store = {}

    def get_bot_state(self, key):
        return self._store.get(key)

    def set_bot_state(self, key, value, ttl=86400):
        self._store[key] = value


def _mock_response(status: int, json_data=None):
    """Create a mock aiohttp response context manager."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    if json_data is not None:
        mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def _mock_session(response):
    """Create a mock aiohttp session."""
    session = AsyncMock()
    session.get = MagicMock(return_value=response)
    return session


# ================================================================
# Fear & Greed
# ================================================================

class TestFearGreed:
    def test_valid_response(self):
        redis = FakeRedis()
        client = NewsClient(redis_store=redis)
        resp = _mock_response(200, {
            "data": [{"value": "23", "value_classification": "Extreme Fear"}]
        })

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            result = asyncio.run(client.fetch_fear_greed())

        assert result == (23, "Extreme Fear")

    def test_api_error_returns_none(self):
        client = NewsClient(redis_store=FakeRedis())
        resp = _mock_response(500)

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            result = asyncio.run(client.fetch_fear_greed())

        assert result is None

    def test_empty_data_returns_none(self):
        client = NewsClient(redis_store=FakeRedis())
        resp = _mock_response(200, {"data": []})

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            result = asyncio.run(client.fetch_fear_greed())

        assert result is None

    def test_exception_returns_none(self):
        client = NewsClient(redis_store=FakeRedis())
        session = AsyncMock()
        session.get = MagicMock(side_effect=Exception("network error"))

        with patch.object(client, '_get_session', return_value=session):
            result = asyncio.run(client.fetch_fear_greed())

        assert result is None

    def test_redis_cache_hit(self):
        redis = FakeRedis()
        redis.set_bot_state("news:fear_greed", json.dumps({"score": 42, "label": "Fear"}))
        client = NewsClient(redis_store=redis)

        result = asyncio.run(client.fetch_fear_greed())
        assert result == (42, "Fear")

    def test_caches_result_in_redis(self):
        redis = FakeRedis()
        client = NewsClient(redis_store=redis)
        resp = _mock_response(200, {
            "data": [{"value": "55", "value_classification": "Greed"}]
        })

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            asyncio.run(client.fetch_fear_greed())

        cached = json.loads(redis.get_bot_state("news:fear_greed"))
        assert cached["score"] == 55
        assert cached["label"] == "Greed"


# ================================================================
# Headlines
# ================================================================

class TestHeadlines:
    def test_valid_response(self):
        client = NewsClient(redis_store=FakeRedis())
        resp = _mock_response(200, {
            "articles": [
                {"title": "BTC hits 100K", "source": "CoinDesk",
                 "pubDate": "", "category": "bitcoin"},
                {"title": "ETH upgrade", "source": "Decrypt",
                 "pubDate": "", "category": "ethereum"},
            ]
        })

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            result = asyncio.run(client.fetch_headlines("BTC", limit=5))

        assert len(result) == 2
        assert result[0].title == "BTC hits 100K"
        assert result[0].source == "CoinDesk"
        assert isinstance(result[0], NewsHeadline)

    def test_403_returns_empty(self):
        client = NewsClient(redis_store=FakeRedis())
        resp = _mock_response(403)

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            result = asyncio.run(client.fetch_headlines("BTC"))

        assert result == []

    def test_empty_articles(self):
        client = NewsClient(redis_store=FakeRedis())
        resp = _mock_response(200, {"articles": []})

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            result = asyncio.run(client.fetch_headlines("ETH"))

        assert result == []

    def test_exception_returns_empty(self):
        client = NewsClient(redis_store=FakeRedis())
        session = AsyncMock()
        session.get = MagicMock(side_effect=Exception("timeout"))

        with patch.object(client, '_get_session', return_value=session):
            result = asyncio.run(client.fetch_headlines("BTC"))

        assert result == []

    def test_redis_cache_hit(self):
        redis = FakeRedis()
        cached = [
            {"title": "Cached headline", "source": "Test",
             "timestamp": 1000, "category": "bitcoin"}
        ]
        redis.set_bot_state("news:headlines:BTC", json.dumps(cached))
        client = NewsClient(redis_store=redis)

        result = asyncio.run(client.fetch_headlines("BTC"))
        assert len(result) == 1
        assert result[0].title == "Cached headline"

    def test_respects_limit(self):
        client = NewsClient(redis_store=FakeRedis())
        articles = [
            {"title": f"Article {i}", "source": "Src", "pubDate": "", "category": "btc"}
            for i in range(10)
        ]
        resp = _mock_response(200, {"articles": articles})

        with patch.object(client, '_get_session', return_value=_mock_session(resp)):
            result = asyncio.run(client.fetch_headlines("BTC", limit=3))

        assert len(result) == 3


# ================================================================
# Combined sentiment
# ================================================================

class TestFetchSentiment:
    def test_returns_none_if_fg_fails(self):
        client = NewsClient(redis_store=FakeRedis())
        with patch.object(client, 'fetch_fear_greed', return_value=None):
            result = asyncio.run(client.fetch_sentiment())
        assert result is None

    def test_returns_sentiment_with_fg_and_headlines(self):
        client = NewsClient(redis_store=FakeRedis())
        headlines = [NewsHeadline("Test", "Src", 1000, "bitcoin")]

        with patch.object(client, 'fetch_fear_greed', return_value=(30, "Fear")), \
             patch.object(client, 'fetch_headlines', return_value=headlines):
            result = asyncio.run(client.fetch_sentiment())

        assert result is not None
        assert result.score == 30
        assert result.label == "Fear"
        assert len(result.headlines) == 2  # BTC + ETH calls

    def test_returns_sentiment_without_headlines(self):
        client = NewsClient(redis_store=FakeRedis())

        with patch.object(client, 'fetch_fear_greed', return_value=(75, "Greed")), \
             patch.object(client, 'fetch_headlines', return_value=[]):
            result = asyncio.run(client.fetch_sentiment())

        assert result is not None
        assert result.score == 75
        assert result.headlines == []

    def test_no_redis_works(self):
        """NewsClient without Redis still fetches correctly."""
        client = NewsClient(redis_store=None)

        with patch.object(client, 'fetch_fear_greed', return_value=(50, "Neutral")), \
             patch.object(client, 'fetch_headlines', return_value=[]):
            result = asyncio.run(client.fetch_sentiment())

        assert result is not None
        assert result.score == 50


# ================================================================
# Pre-filter integration
# ================================================================

class TestPreFilterIntegration:
    """Test the Fear & Greed pre-filter logic from main.py."""

    def _pre_filter_fg(self, direction: str, fg_score: int) -> str | None:
        """Simulate the F&G pre-filter check."""
        if direction == "long" and fg_score < settings.NEWS_EXTREME_FEAR_THRESHOLD:
            return f"Extreme Fear (F&G={fg_score}) — rejecting long"
        if direction == "short" and fg_score > settings.NEWS_EXTREME_GREED_THRESHOLD:
            return f"Extreme Greed (F&G={fg_score}) — rejecting short"
        return None

    def test_extreme_fear_rejects_long(self):
        result = self._pre_filter_fg("long", 8)
        assert result is not None
        assert "Extreme Fear" in result

    def test_extreme_fear_allows_short(self):
        assert self._pre_filter_fg("short", 8) is None

    def test_extreme_greed_rejects_short(self):
        result = self._pre_filter_fg("short", 90)
        assert result is not None
        assert "Extreme Greed" in result

    def test_extreme_greed_allows_long(self):
        assert self._pre_filter_fg("long", 90) is None

    def test_normal_allows_both(self):
        assert self._pre_filter_fg("long", 50) is None
        assert self._pre_filter_fg("short", 50) is None

    def test_boundary_fear_allows(self):
        # Score == threshold should NOT reject (< not <=)
        assert self._pre_filter_fg("long", settings.NEWS_EXTREME_FEAR_THRESHOLD) is None

    def test_boundary_greed_allows(self):
        assert self._pre_filter_fg("short", settings.NEWS_EXTREME_GREED_THRESHOLD) is None


# ================================================================
# Timestamp parsing
# ================================================================

class TestParseTimestamp:
    def test_empty_returns_fallback(self):
        assert _parse_timestamp("", 999) == 999

    def test_rfc2822_parses(self):
        result = _parse_timestamp("Mon, 09 Mar 2026 12:00:00 +0000", 0)
        assert result > 0

    def test_invalid_returns_fallback(self):
        assert _parse_timestamp("not-a-date", 42) == 42
