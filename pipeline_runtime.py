"""Shared runtime state for the trading pipeline.

A single `rt` object holds every service reference and mutable cross-call
state that the pipeline callback and its helpers need. It exists so that, as
`main.py` is split into focused modules (Refactor Phase 6,
`docs/plans/main-py-split-phase6.md`), every module can reach the same live
state via `from pipeline_runtime import rt` instead of importing module-level
globals from `main` — which would NOT propagate, because reassigning a module
global imported by value does not update the importer's binding. Attribute
assignment on a shared object (`rt.data_service = ...`) propagates everywhere.

This phase introduces the holder and repoints `main.py` at it WITHOUT moving
any pipeline function. Behaviour is unchanged: every field keeps the exact
default and mutate-between-calls semantics it had as a module global.

Service classes are imported only under TYPE_CHECKING so this module stays
import-cheap and free of cycles (the service modules do not import this one).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_service.service import DataService
    from strategy_service import StrategyService
    from ai_service import AIService
    from risk_service import RiskService
    from execution_service import ExecutionService
    from execution_service.campaign_monitor import CampaignMonitor
    from execution_service.shadow_monitor import ShadowMonitor
    from execution_service.dual_thrust_shadow import DualThrustShadowTracker
    from shared.notifier import TelegramNotifier
    from shared.alert_manager import AlertManager


class Runtime:
    """Live pipeline state. One process-wide instance (`rt`).

    Services are set by `main()` during startup; they are `None` until then,
    and every pipeline helper guards on that exactly as before. State fields
    carry the same initial values the former module globals had.
    """

    def __init__(self) -> None:
        # --- Service references (set by main() at startup) ---
        self.data_service: "DataService | None" = None
        self.strategy_service: "StrategyService | None" = None
        self.ai_service: "AIService | None" = None
        self.risk_service: "RiskService | None" = None
        self.execution_service: "ExecutionService | None" = None
        self.campaign_monitor: "CampaignMonitor | None" = None
        self.shadow_monitor: "ShadowMonitor | None" = None
        self.dual_thrust_shadow: "DualThrustShadowTracker | None" = None
        self.notifier: "TelegramNotifier | None" = None
        self.alert_manager: "AlertManager | None" = None

        # --- Setup dedup cache ---
        # Key: (pair, direction, setup_type), Value: unix ts of last eval.
        self.setup_dedup_cache: dict[tuple, float] = {}

        # --- Dry-spell / detection tracking ---
        self.last_setup_detected_time: float = 0.0
        self.atr_history: dict[str, list[float]] = {}  # pair -> recent ATR values
        self.bot_start_time: float = 0.0
        self.dry_spell_alerted: bool = False

        # --- engine1 kill-switch alert throttle ---
        self.engine1_kill_alert_ts: float = 0.0

        # --- Market-monitor per-pair alert cooldowns ---
        self.vol_spike_cooldown: dict[str, float] = {}      # pair -> last alert time
        self.funding_extreme_cooldown: dict[str, float] = {}  # pair -> last alert time

        # --- emit_metric failure surfacing ---
        self.emit_metric_failures: int = 0
        self.emit_metric_last_warn: float = 0.0


# Process-wide singleton. Import this, never re-instantiate.
rt = Runtime()
