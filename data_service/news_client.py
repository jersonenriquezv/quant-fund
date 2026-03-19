"""
News sentiment client — Fear & Greed Index + crypto headlines.

Two sources:
1. alternative.me — Fear & Greed Index (0-100 score, free, no API key)
2. CryptoCompare — News headlines (free, no API key, JSON REST)

Both sources are optional — graceful degradation on failure.
Redis caching prevents hammering the APIs.
"""

import asyncio
import json
import time
from typing import Optional

import aiohttp

from config.settings import settings
from shared.logger import setup_logger
from shared.models import NewsHeadline, NewsSentiment

logger = setup_logger("news_client")

_USER_AGENT = "QuantFundBot/1.0"
_REQUEST_TIMEOUT = 15  # seconds
_CRYPTOCOMPARE_NEWS = "https://min-api.cryptocompare.com/data/v2/news/"


class NewsClient:
    """Fetches news sentiment data from external APIs."""

    def __init__(self, redis_store=None):
        """
        Args:
            redis_store: Optional RedisStore for caching. If None, no caching.
        """
        self._redis = redis_store
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
                headers={"User-Agent": _USER_AGENT},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ================================================================
    # Fear & Greed Index (alternative.me)
    # ================================================================

    async def fetch_fear_greed(self) -> Optional[tuple[int, str]]:
        """Fetch Fear & Greed Index. Returns (score, label) or None on error.

        Checks Redis cache first (TTL = NEWS_FEAR_GREED_CACHE_TTL).
        """
        # Check cache
        cached = self._get_cached("news:fear_greed")
        if cached is not None:
            try:
                data = json.loads(cached)
                return (data["score"], data["label"])
            except (json.JSONDecodeError, KeyError):
                pass

        try:
            session = await self._get_session()
            url = f"{settings.NEWS_FEAR_GREED_URL}?limit=1"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"Fear & Greed API returned {resp.status}")
                    return None
                body = await resp.json(content_type=None)

            data = body.get("data", [])
            if not data:
                logger.warning("Fear & Greed API returned empty data")
                return None

            entry = data[0]
            score = int(entry["value"])
            label = entry.get("value_classification", "Unknown")

            # Cache result
            self._set_cached(
                "news:fear_greed",
                json.dumps({"score": score, "label": label}),
                ttl=settings.NEWS_FEAR_GREED_CACHE_TTL,
            )

            logger.debug(f"Fear & Greed: {score} ({label})")
            return (score, label)

        except asyncio.TimeoutError:
            logger.warning("Fear & Greed API timeout")
            return None
        except Exception as e:
            logger.warning(f"Fear & Greed fetch failed: {e}")
            return None

    # ================================================================
    # Headlines (CryptoCompare)
    # ================================================================

    async def fetch_headlines(self, asset: str = "BTC", limit: int = 5) -> list[NewsHeadline]:
        """Fetch recent news headlines from CryptoCompare.

        Free API, no key required. Filters by asset category.
        Checks Redis cache first (TTL = NEWS_HEADLINES_CACHE_TTL).
        """
        cache_key = f"news:headlines:{asset}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            try:
                items = json.loads(cached)
                return [
                    NewsHeadline(
                        title=h["title"],
                        source=h["source"],
                        timestamp=h["timestamp"],
                        category=h["category"],
                        url=h.get("url", ""),
                        sentiment=h.get("sentiment"),
                    )
                    for h in items
                ]
            except (json.JSONDecodeError, KeyError):
                pass

        try:
            session = await self._get_session()
            url = f"{_CRYPTOCOMPARE_NEWS}?lang=EN&categories={asset}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"CryptoCompare news API returned {resp.status} for {asset}")
                    return []
                body = await resp.json(content_type=None)

            articles = body.get("Data") or []
            if not isinstance(articles, list):
                msg = body.get("Message", "unknown")
                logger.warning(f"CryptoCompare news: unexpected Data for {asset}: {msg}")
                return []

            headlines = []
            for art in articles[:limit]:
                sentiment = _extract_sentiment(
                    art.get("upvotes", 0), art.get("downvotes", 0)
                )
                headlines.append(NewsHeadline(
                    title=art.get("title", ""),
                    source=art.get("source", "Unknown"),
                    timestamp=art.get("published_on", 0) * 1000,  # seconds → ms
                    category=asset,
                    url=art.get("url", ""),
                    sentiment=sentiment,
                ))

            # Cache result
            cache_data = [
                {"title": h.title, "source": h.source,
                 "timestamp": h.timestamp, "category": h.category,
                 "url": h.url, "sentiment": h.sentiment}
                for h in headlines
            ]
            self._set_cached(cache_key, json.dumps(cache_data),
                             ttl=settings.NEWS_HEADLINES_CACHE_TTL)

            logger.debug(f"CryptoCompare: fetched {len(headlines)} headlines for {asset}")
            return headlines

        except asyncio.TimeoutError:
            logger.warning(f"CryptoCompare news API timeout for {asset}")
            return []
        except Exception as e:
            logger.warning(f"CryptoCompare news fetch failed for {asset}: {e}")
            return []

    # ================================================================
    # Combined fetch
    # ================================================================

    async def fetch_sentiment(self) -> Optional[NewsSentiment]:
        """Fetch Fear & Greed + headlines, combine into NewsSentiment.

        Returns None only if Fear & Greed fails (headlines are optional).
        """
        fg = await self.fetch_fear_greed()
        if fg is None:
            return None

        score, label = fg

        # Fetch headlines for both assets
        btc_headlines = await self.fetch_headlines("BTC", limit=3)
        eth_headlines = await self.fetch_headlines("ETH", limit=2)
        all_headlines = btc_headlines + eth_headlines

        return NewsSentiment(
            score=score,
            label=label,
            headlines=all_headlines,
            fetched_at=int(time.time() * 1000),
        )

    # ================================================================
    # Redis cache helpers
    # ================================================================

    def _get_cached(self, key: str) -> Optional[str]:
        if self._redis is None:
            return None
        try:
            return self._redis.get_bot_state(key)
        except Exception:
            return None

    def _set_cached(self, key: str, value: str, ttl: int) -> None:
        if self._redis is None:
            return
        try:
            self._redis.set_bot_state(key, value, ttl=ttl)
        except Exception:
            pass


def _extract_sentiment(upvotes: int, downvotes: int) -> Optional[str]:
    """Derive sentiment from CryptoCompare community votes.

    Returns "bullish", "bearish", or None if no clear signal.
    """
    if upvotes == 0 and downvotes == 0:
        return None
    if upvotes > downvotes:
        return "bullish"
    if downvotes > upvotes:
        return "bearish"
    return None
