"""
AI Service facade — Claude-powered trade filter.

Takes TradeSetup + MarketSnapshot, asks Claude to evaluate,
returns AIDecision. If anything fails, rejects the trade (fail-safe).
"""

from config.settings import settings
from shared.logger import setup_logger
from shared.models import TradeSetup, MarketSnapshot, AIDecision
from ai_service.prompt_builder import PromptBuilder, PROMPT_VERSION
from ai_service.claude_client import ClaudeClient

logger = setup_logger("ai_service")


class AIService:
    """Layer 3 — Claude API as trade filter. Does not originate trades."""

    def __init__(self, data_service=None) -> None:
        self._data = data_service
        self._prompt_builder = PromptBuilder()
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

        # Log prompt for audit trail
        logger.debug(f"Claude prompt for {setup.pair} {setup.direction}:\n{user_prompt}")

        # Build system prompt per call (threshold changes with profile)
        system_prompt = self._prompt_builder.build_system_prompt()

        # Call Claude
        result = await self._claude.evaluate(system_prompt, user_prompt)

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

        # Construct reasoning from structured factors
        supporting = result.get("supporting_factors") or []
        contradicting = result.get("contradicting_factors") or []
        scores = result.get("scores") or {}

        reasoning_parts = []
        if supporting:
            reasoning_parts.append("Supporting: " + "; ".join(supporting))
        if contradicting:
            reasoning_parts.append("Against: " + "; ".join(contradicting))
        reasoning = " | ".join(reasoning_parts) if reasoning_parts else "No factors provided"

        # Store scores and prompt version alongside SL/TP adjustments
        adjustments = result.get("adjustments") or {}
        adjustments["scores"] = scores
        adjustments["prompt_version"] = PROMPT_VERSION

        decision = AIDecision(
            confidence=confidence,
            approved=approved,
            reasoning=reasoning,
            adjustments=adjustments,
            warnings=result.get("warnings") or [],
        )

        status = "APPROVED" if decision.approved else "REJECTED"
        scores_str = " ".join(f"{k}={v}" for k, v in scores.items())
        logger.info(
            f"AI {status}: pair={setup.pair} direction={setup.direction} "
            f"confidence={decision.confidence:.2f} "
            f"scores=[{scores_str}] "
            f"reasoning={decision.reasoning}"
        )

        return decision

    def _get_candles_context(self, pair: str) -> dict:
        """Get recent candle data for price change context (1h/4h)."""
        context = {}
        if self._data is None:
            return context

        timeframes = ["1h", "4h"]
        for tf in timeframes:
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
