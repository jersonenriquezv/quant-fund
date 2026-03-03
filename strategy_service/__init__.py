"""
Strategy Service (Layer 2) — SMC pattern detection engine.

Deterministic rules. No AI. Detects Smart Money Concepts patterns
on incoming candle data and produces TradeSetup objects.
"""

from strategy_service.service import StrategyService

__all__ = ["StrategyService"]
