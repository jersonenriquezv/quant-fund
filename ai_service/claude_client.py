"""
Anthropic Claude API wrapper.

Handles authentication, timeouts, retries, JSON parsing, and error handling.
Returns parsed dict on success, None on any failure (fail-safe).
"""

import json
import re

from anthropic import AsyncAnthropic, APIError, APITimeoutError, RateLimitError

from config.settings import settings
from shared.logger import setup_logger

logger = setup_logger("ai_service")


class ClaudeClient:
    """Async wrapper around the Anthropic Messages API."""

    def __init__(self) -> None:
        api_key = settings.ANTHROPIC_API_KEY
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in .env")

        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=settings.AI_TIMEOUT_SECONDS,
            max_retries=2,
        )
        self._model = settings.CLAUDE_MODEL

    async def evaluate(self, system_prompt: str, user_prompt: str) -> dict | None:
        """Send setup evaluation to Claude and parse JSON response.

        Returns parsed dict on success, None on any failure.
        """
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=settings.AI_MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
                temperature=settings.AI_TEMPERATURE,
            )

            # Log token usage for cost tracking
            usage = response.usage
            logger.info(
                f"Claude tokens: input={usage.input_tokens} "
                f"output={usage.output_tokens} "
                f"total={usage.input_tokens + usage.output_tokens}"
            )

            if not response.content:
                logger.error("Claude returned empty response.content")
                return None
            raw_text = response.content[0].text

            # Extract JSON from response — handles preamble text + code fences
            cleaned = raw_text.strip()
            fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", cleaned, re.DOTALL)
            if fence_match:
                cleaned = fence_match.group(1).strip()
            elif not cleaned.startswith("{"):
                # No fence, no leading brace — try to find first JSON object
                brace_idx = cleaned.find("{")
                if brace_idx >= 0:
                    cleaned = cleaned[brace_idx:]

            parsed = json.loads(cleaned)

            # Validate required fields
            required = {"confidence", "approved", "reasoning"}
            missing = required - set(parsed.keys())
            if missing:
                logger.error(f"Claude response missing fields: {missing}")
                return None

            if not isinstance(parsed["confidence"], (int, float)):
                logger.error(f"confidence is not numeric: {type(parsed['confidence'])}")
                return None
            if not isinstance(parsed["approved"], bool):
                logger.error(f"approved is not bool: {type(parsed['approved'])}")
                return None

            return parsed

        except json.JSONDecodeError as e:
            logger.error(f"Claude returned invalid JSON: {e}")
            return None
        except APITimeoutError:
            logger.error(f"Claude API timeout ({settings.AI_TIMEOUT_SECONDS}s)")
            return None
        except RateLimitError as e:
            logger.warning(f"Claude API rate limited: {e}")
            return None
        except APIError as e:
            logger.error(f"Claude API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error calling Claude: {e}")
            return None

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.close()
