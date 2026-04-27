"""Redesign engines (docs/strategy_redesign_2026_04.md §4).

Each engine is a self-contained signal module with its own thesis,
detection logic, entry / SL / TP rules, and gates. Engines emit
`TradeSetup` objects via `evaluate(...)` and are registered in
`StrategyService._iterate_setups`. Engines do NOT reuse the legacy
`_apply_expectancy_filters` — owning their own gates is part of the
contract that distinguishes them from setups.py.
"""
