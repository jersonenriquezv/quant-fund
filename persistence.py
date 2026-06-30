"""Fire-and-forget persistence writers + operational metrics.

Leaf helpers extracted from main.py (Refactor Phase 6,
docs/plans/main-py-split-phase6.md). They write audit/journal rows and
Grafana metrics to PostgreSQL and never raise into the pipeline — every
failure is logged and swallowed. All live state (the DataService handle,
the emit-metric failure counters) is reached via the shared `rt` singleton,
so behaviour is identical to when these lived in main.

Pure relocation: function bodies are unchanged.
"""

import time

from pipeline_runtime import rt
from shared.logger import setup_logger

logger = setup_logger("persistence")


# ================================================================
# Metrics helper (fire-and-forget to PostgreSQL for Grafana)
# ================================================================

# In-memory counter of emit_metric failures — surfaced periodically so a
# silent Postgres outage does not remain invisible indefinitely.


def _emit_metric(name: str, value: float, pair: str | None = None, labels: dict | None = None) -> None:
    """Write an operational metric to PostgreSQL. Non-blocking.

    Errors are counted in-memory and surfaced via a WARNING every 5 minutes
    (max) so a broken metrics path does not silently hide all observability.
    """
    if rt.data_service is None:
        return
    try:
        rt.data_service.postgres.insert_metric(name, value, pair=pair, labels=labels)
    except Exception as e:
        rt.emit_metric_failures += 1
        now = time.time()
        if now - rt.emit_metric_last_warn > 300:
            logger.warning(
                f"_emit_metric failures: {rt.emit_metric_failures} since last warn "
                f"(last error: {e}). Metrics path may be degraded."
            )
            rt.emit_metric_last_warn = now


# ================================================================
# Persistence helpers (called from pipeline callback)
# ================================================================

def _persist_ai_decision(trade_id, decision, setup) -> None:
    """Write AI decision to PostgreSQL (fire-and-forget)."""
    if rt.data_service is None:
        return
    try:
        rt.data_service.postgres.insert_ai_decision(
            trade_id=trade_id,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            adjustments=decision.adjustments,
            warnings=list(decision.warnings),
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            approved=decision.approved,
        )
    except Exception as e:
        logger.error(f"Failed to persist AI decision: {e}")


def _persist_ai_pre_filter(setup, reason: str) -> None:
    """Write synthetic AI decision for pre-filter rejection (audit trail)."""
    if rt.data_service is None:
        return
    try:
        rt.data_service.postgres.insert_ai_decision(
            trade_id=None,
            confidence=0.0,
            reasoning=f"Pre-filter: {reason}",
            adjustments=None,
            warnings=[],
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            approved=False,
        )
    except Exception as e:
        logger.error(f"Failed to persist AI pre-filter: {e}")


def _persist_risk_event(event_type: str, details: dict) -> None:
    """Write risk event to PostgreSQL (fire-and-forget)."""
    if rt.data_service is None:
        return
    try:
        rt.data_service.postgres.insert_risk_event(event_type, details)
    except Exception as e:
        logger.error(f"Failed to persist risk event: {e}")


def _log_trade_rejection(setup, reason: str) -> None:
    """Log a rejected trade to the trade_rejections table (fire-and-forget).

    Defensive math: guard every denominator. Rejected setups occasionally
    have malformed prices (entry=0 from a stale snapshot, sl==entry from
    an ATR collapse) and a single ZeroDivisionError would swallow the log.
    """
    if rt.data_service is None:
        return
    try:
        entry = setup.entry_price if setup.entry_price > 0 else 0.0
        risk = abs(setup.entry_price - setup.sl_price)
        sl_dist = (risk / entry) if entry > 0 else None
        rr = abs(setup.tp2_price - setup.entry_price) / risk if risk > 0 else None
        rt.data_service.postgres.insert_trade_rejection(
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            reason=reason,
            sl_distance_pct=sl_dist,
            rr_ratio=rr,
        )
    except Exception as e:
        logger.error(f"Failed to log trade rejection: {e}")
