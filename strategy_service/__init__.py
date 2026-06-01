"""
Strategy Service (Layer 2) — SMC pattern detection engine.

Deterministic rules. No AI. Detects Smart Money Concepts patterns
on incoming candle data and produces TradeSetup objects.
"""

# Lazy export: importing a light submodule (e.g. strategy_service.market_structure)
# must NOT pull in service.py and its heavy deps (anthropic/ccxt). The dashboard
# API imports only the pure detector modules; eager-importing StrategyService here
# would drag the whole bot stack into that image. main.py's
# `from strategy_service import StrategyService` still works via PEP 562.
__all__ = ["StrategyService"]


def __getattr__(name: str):
    if name == "StrategyService":
        from strategy_service.service import StrategyService
        return StrategyService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
