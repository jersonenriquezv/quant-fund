"""
AI Service facade — Claude-powered trade filter.

Takes TradeSetup + MarketSnapshot, asks Claude to evaluate,
returns AIDecision. If anything fails, rejects the trade (fail-safe).
"""

from config.settings import settings
from shared.logger import setup_logger
from shared.models import TradeSetup, MarketSnapshot, AIDecision
from ai_service.prompt_builder import PromptBuilder
from ai_service.claude_client import ClaudeClient

logger = setup_logger("ai_service")


class AIService:
    """Layer 3 — Claude API as trade filter. Does not originate trades."""

    def __init__(self, data_service=None) -> None:
        self._data = data_service
        self._prompt_builder = PromptBuilder()
        self._system_prompt = self._prompt_builder.build_system_prompt()
        self._enabled = bool(settings.ANTHROPIC_API_KEY)

        if not self._enabled:
            logger.warning(
                "AI Service DISABLED — ANTHROPIC_API_KEY not set. "
                "All setups will be auto-rejected."
            )
            self._claude = None
        else:
            self._claude = ClaudeClient()
            logger.info(
                f"AI Service initialized — model={settings.CLAUDE_MODEL} "
                f"min_confidence={settings.AI_MIN_CONFIDENCE}"
            )

    async def evaluate(
        self, setup: TradeSetup, snapshot: MarketSnapshot
    ) -> AIDecision:
        """Evaluate a trade setup using Claude.

        Returns AIDecision. On any failure, returns approved=False (fail-safe).
        """
        if not self._enabled:
            return AIDecision(
                confidence=0.0,
                approved=False,
                reasoning="AI Service disabled — ANTHROPIC_API_KEY not configured",
                adjustments={},
                warnings=["AI Service not available"],
            )

        # Build candles context for price change
        candles_context = self._get_candles_context(setup.pair)

        # Build prompt
        user_prompt = self._prompt_builder.build_evaluation_prompt(
            setup, snapshot, candles_context
        )

        # Call Claude
        result = await self._claude.evaluate(self._system_prompt, user_prompt)

        # Handle API failure
        if result is None:
            logger.warning(
                f"Claude API call failed — rejecting {setup.pair} {setup.direction}"
            )
            return AIDecision(
                confidence=0.0,
                approved=False,
                reasoning="Claude API call failed — trade rejected for safety",
                adjustments={},
                warnings=["API failure — auto-rejected"],
            )

        # Build AIDecision from parsed response
        confidence = float(result.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        # Double check: approved=true AND confidence >= threshold
        approved = (
            result.get("approved", False) is True
            and confidence >= settings.AI_MIN_CONFIDENCE
        )

        decision = AIDecision(
            confidence=confidence,
            approved=approved,
            reasoning=result.get("reasoning", "No reasoning provided"),
            adjustments=result.get("adjustments") or {},
            warnings=result.get("warnings") or [],
        )

        status = "APPROVED" if decision.approved else "REJECTED"
        logger.info(
            f"AI {status}: pair={setup.pair} direction={setup.direction} "
            f"confidence={decision.confidence:.2f} "
            f"reasoning={decision.reasoning}"
        )

        return decision

    def _get_candles_context(self, pair: str) -> dict:
        """Get recent candle data for price change context."""
        context = {}
        if self._data is None:
            return context

        for tf in ["1h", "4h"]:
            candles = self._data.get_candles(pair, tf, 10)
            if candles and len(candles) >= 2:
                latest = candles[-1].close
                prev = candles[-2].close
                pct_change = ((latest - prev) / prev) * 100
                context[tf] = {
                    "latest_close": latest,
                    "prev_close": prev,
                    "pct_change": round(pct_change, 3),
                }
        return context

    async def close(self) -> None:
        """Close the Claude API client."""
        if self._claude is not None:
            await self._claude.close()
