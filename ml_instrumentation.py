"""ML instrumentation + engine1 meta-label scoring/kill-switch helpers.

Extracted from main.py (Refactor Phase 6, docs/plans/main-py-split-phase6.md).
These capture the feature snapshot at detection (`_ml_log_setup`), resolve the
terminal outcome (`_ml_resolve_outcome`), and run the engine1 frozen-model
score + live-gate kill switch. All are fire-and-forget — a logging/scoring
failure is logged and swallowed, never blocking the pipeline. State/services
reached via the shared `rt` singleton.

Pure relocation: function bodies are unchanged.
"""

import time

from config.settings import settings
from shared.logger import setup_logger
from shared.ml_features import extract_setup_features, extract_risk_context
from shared.alert_manager import AlertPriority
from shared.models import Candle
from pipeline_runtime import rt
from persistence import _emit_metric

logger = setup_logger("ml_instrumentation")


def _ml_log_setup(setup, candle: Candle) -> dict | None:
    """Log setup features to ml_setups table at detection time.

    Returns the feature dict captured at detection (for in-process scoring),
    or None if logging was skipped or failed.
    """
    if rt.data_service is None or rt.data_service.postgres is None:
        return None
    try:
        snapshot = rt.data_service.get_market_snapshot(candle.pair)
        current_price = candle.close
        recent_candles = rt.data_service.get_candles(candle.pair, candle.timeframe, count=100)

        # Orderbook snapshot for spread/imbalance features
        ob_snapshot = None
        try:
            ob_snapshot = rt.data_service.get_orderbook_snapshot(candle.pair)
        except Exception as e:
            logger.debug(f"orderbook snapshot miss ({candle.pair}): {e}")

        # BTC candles for correlation features (altcoins only)
        btc_candles = None
        if candle.pair != "BTC/USDT":
            try:
                btc_candles = rt.data_service.get_candles("BTC/USDT", candle.timeframe, count=50)
            except Exception as e:
                logger.debug(f"BTC candle fetch miss ({candle.pair}): {e}")

        features = extract_setup_features(
            setup, snapshot, current_price, recent_candles,
            ob_snapshot=ob_snapshot, btc_candles=btc_candles,
        )
        # Add fields that come from setup but aren't in the feature dict yet
        features["timestamp"] = setup.timestamp
        features["tp1_price"] = setup.tp1_price
        features["tp2_price"] = setup.tp2_price

        # Risk context at detection time (before risk check).
        # Shadow setups: override risk_capital with SHADOW_CAPITAL so the
        # ml_setups row reflects the virtual capital the shadow will size
        # against, not live OKX balance. Keeps risk_capital consistent with
        # shadow_position_size and shadow_margin on the same row.
        risk_ctx = None
        if rt.risk_service is not None:
            is_shadow = setup.setup_type in settings.SHADOW_MODE_SETUPS
            override = settings.effective_shadow_capital if is_shadow else None
            risk_ctx = extract_risk_context(rt.risk_service, capital_override=override)

        # Scalp setups live under their own experiment_id so v1/v2/v3 datasets
        # stay separable from engine1/swing experiments. Without this branch,
        # bumping SCALP_EXPERIMENT_ID has no effect on inserts — only on
        # report scripts — and v2 filter changes get silently mixed with the
        # active engine1 EXPERIMENT_ID. See PR fix/scalp-experiment-id-wiring.
        is_scalp = setup.setup_type in settings.SCALP_SETUP_TYPES
        experiment_id = settings.SCALP_EXPERIMENT_ID if is_scalp else settings.EXPERIMENT_ID

        ok = rt.data_service.postgres.insert_ml_setup(
            setup_id=setup.setup_id,
            features=features,
            risk_context=risk_ctx,
            feature_version=settings.ML_FEATURE_VERSION,
            experiment_id=experiment_id,
        )
        _emit_metric("ml_setup_insert_ok" if ok else "ml_setup_insert_error", 1, setup.pair)
        return features
    except Exception as e:
        logger.error(f"ML setup logging failed: {e}")
        _emit_metric("ml_setup_insert_error", 1, setup.pair)
        return None


def _engine1_score_log(setup, features: dict) -> float | None:
    """Score an engine1 setup with the frozen meta-label model and log it.

    Surfaces the score + cutoff decision per emission. Returns the score (P(tp)
    in [0,1]) for the live-gate decision, or None if scoring failed. Logging is
    always best-effort — a scoring failure never blocks the pipeline.
    """
    try:
        from strategy_service.engines import engine1_scorer
        score = engine1_scorer.score_features(features)
        eligible = engine1_scorer.passes_cutoff(score)
        gate = "ON" if settings.ENGINE1_LIVE_GATED_ENABLED else "OFF"
        logger.info(
            f"ENGINE1_LIVE_SCORE {setup.pair} {setup.direction} "
            f"score={score:.4f} cutoff={settings.ENGINE1_SCORE_CUTOFF} "
            f"eligible={eligible} live_gate={gate}"
        )
        _emit_metric("engine1_score", score, setup.pair)
        return score
    except Exception as e:
        logger.error(f"engine1 scoring failed ({setup.pair}): {e}")
        return None


# Throttle for the kill-switch alert — avoid spamming on every eligible emission.
_ENGINE1_KILL_ALERT_TTL = 3600  # seconds


def _engine1_kill_check() -> tuple[bool, str | None]:
    """Evaluate the engine1 live-gate kill switch over closed engine1 trades.

    Returns (triggered, reason). Fail-safe: on any error returns (False, None)
    so a transient DB hiccup never blocks live entries on its own — the
    standalone guardrails (DD, cooldown) still protect capital.
    """
    if rt.data_service is None or rt.data_service.postgres is None:
        return False, None
    try:
        from strategy_service.engines import engine1_kill_switch

        # Pull enough history to cover the rolling window + DD curve.
        limit = max(settings.ENGINE1_KILL_ROLLING_WINDOW * 3, 60)
        rows = rt.data_service.postgres.fetch_recent_closed_trades(limit=limit)
        # fetch_recent_closed_trades returns DESC; kill-switch needs oldest-first.
        pnls = [
            float(r["pnl_usd"])
            for r in reversed(rows)
            if r.get("setup_type") == "engine1_trend_pullback"
            and r.get("pnl_usd") is not None
        ]
        if not pnls:
            return False, None
        verdict = engine1_kill_switch.evaluate_kill(
            pnls,
            r_usd=settings.ENGINE1_RISK_USD,
            dd_r_limit=settings.ENGINE1_KILL_DD_R,
            consec_limit=settings.ENGINE1_KILL_CONSEC_LOSSES,
            pf_floor=settings.ENGINE1_KILL_ROLLING_PF,
            pf_window=settings.ENGINE1_KILL_ROLLING_WINDOW,
        )
        return verdict.triggered, verdict.reason
    except Exception as e:
        logger.error(f"engine1 kill check failed: {e}")
        return False, None


async def _engine1_emit_kill_alert(reason: str) -> None:
    """Fire a throttled CRITICAL Telegram alert when the kill switch trips."""
    now = time.time()
    if (now - rt.engine1_kill_alert_ts) < _ENGINE1_KILL_ALERT_TTL:
        return
    rt.engine1_kill_alert_ts = now
    msg = (
        f"🛑 ENGINE1 KILL SWITCH TRIPPED — {reason}. New engine1 live entries "
        f"reverted to SHADOW automatically. Set ENGINE1_LIVE_GATED_ENABLED=false "
        f"to make it permanent and review."
    )
    logger.critical(msg)
    if rt.alert_manager is not None:
        await rt.alert_manager.alert(AlertPriority.CRITICAL, "engine1_kill", msg)


def _ml_resolve_outcome(setup_id: str, outcome_type: str, **kwargs) -> None:
    """Resolve an ml_setup outcome (fire-and-forget)."""
    if not setup_id or rt.data_service is None or rt.data_service.postgres is None:
        return
    try:
        ok = rt.data_service.postgres.update_ml_setup_outcome(
            setup_id=setup_id,
            outcome_type=outcome_type,
            **kwargs,
        )
        _emit_metric("ml_outcome_update_ok" if ok else "ml_outcome_update_error", 1)
    except Exception as e:
        logger.error(f"ML outcome resolution failed: {setup_id} {e}")
        _emit_metric("ml_outcome_update_error", 1)
