"""
Entry point for the One-Man Quant Fund trading bot.

Single Python process running all 5 layers:
    Data Service → Strategy Service → AI Service → Risk Service → Execution Service

Runs in Docker via docker-compose.yml (bot + PostgreSQL + Redis).

Usage:
    python main.py
"""

import asyncio
import json
import signal
import sys

from config.settings import settings, STRATEGY_PROFILES, apply_profile, reset_profile
from shared.logger import setup_logger
from shared.models import Candle
from data_service.service import DataService
from strategy_service import StrategyService
from ai_service import AIService
from risk_service import RiskService
from execution_service import ExecutionService
from shared.notifier import TelegramNotifier

logger = setup_logger("main")

# Module-level references set by main() so the callback can access them
_data_service: DataService | None = None
_strategy_service: StrategyService | None = None
_ai_service: AIService | None = None
_risk_service: RiskService | None = None
_execution_service: ExecutionService | None = None
_notifier: TelegramNotifier | None = None


# ================================================================
# Pipeline callback — triggered on every confirmed candle
# ================================================================

async def _sync_profile_from_redis() -> None:
    """Check Redis for a profile change from dashboard and apply it."""
    if _data_service is None:
        return
    try:
        stored = _data_service.redis.get_bot_state("strategy_profile")
        if stored and stored in STRATEGY_PROFILES:
            if stored != settings.STRATEGY_PROFILE:
                reset_profile(settings)
                apply_profile(settings, stored)
                logger.info(f"Strategy profile switched to: {stored}")
    except Exception:
        pass  # Redis down — keep current profile


def _publish_strategy_state(pair: str) -> None:
    """Publish active OBs and HTF bias to Redis for the dashboard."""
    if _strategy_service is None or _data_service is None:
        return
    try:
        redis = _data_service.redis

        # Collect OBs for all pairs
        all_obs = []
        for p in settings.TRADING_PAIRS:
            for ob in _strategy_service.get_active_order_blocks(p):
                all_obs.append({
                    "timestamp": ob.timestamp,
                    "pair": ob.pair,
                    "timeframe": ob.timeframe,
                    "direction": ob.direction,
                    "high": ob.high,
                    "low": ob.low,
                    "body_high": ob.body_high,
                    "body_low": ob.body_low,
                    "entry_price": ob.entry_price,
                    "volume_ratio": ob.volume_ratio,
                })
        redis.set_bot_state("order_blocks", json.dumps(all_obs), ttl=600)

        # HTF bias for all pairs
        bias = {p: _strategy_service.get_htf_bias(p) for p in settings.TRADING_PAIRS}
        redis.set_bot_state("htf_bias", json.dumps(bias), ttl=600)
    except Exception as e:
        logger.error(f"Failed to publish strategy state to Redis: {e}")


async def on_candle_confirmed(candle: Candle) -> None:
    """Pipeline entry point: Data → Strategy → AI → Risk → Execution.

    Strategy Service evaluates LTF candles for SMC setups.
    AI/Risk/Execution layers are stubs — will be wired as they're built.
    """
    # Check for profile changes from dashboard
    await _sync_profile_from_redis()

    logger.info(
        f"Pipeline triggered: pair={candle.pair} tf={candle.timeframe} "
        f"close={candle.close} vol={candle.volume:.4f}"
    )

    # Layer 2: Strategy Service — detect SMC setups
    if _strategy_service is None:
        return

    setup = _strategy_service.evaluate(candle.pair, candle)
    _publish_strategy_state(candle.pair)
    if setup is None:
        return

    logger.info(
        f"Trade setup detected: type={setup.setup_type} pair={setup.pair} "
        f"direction={setup.direction} entry={setup.entry_price:.2f} "
        f"sl={setup.sl_price:.2f} tp1={setup.tp1_price:.2f} "
        f"confluences={setup.confluences}"
    )

    # Telegram: setup detected
    if _notifier is not None:
        await _notifier.notify_setup_detected(setup)

    # Layer 3: AI Service — Claude filter (hybrid scalping + pre-filter)
    decision = None
    if settings.STRATEGY_PROFILE == "scalping":
        # Hybrid scalping: HTF-aligned scalps go to Claude, pure LTF scalps bypass
        htf_bias = _strategy_service.get_htf_bias(setup.pair)
        htf_aligned = (
            (setup.direction == "long" and htf_bias == "bullish")
            or (setup.direction == "short" and htf_bias == "bearish")
        )
        if htf_aligned:
            # HTF-aligned scalp — worth Claude's analysis
            logger.info(f"Scalping HTF-aligned ({setup.direction}/{htf_bias}) — sending to Claude")
            decision = await _evaluate_with_claude(setup, candle)
            if decision is None:
                return  # pre-filter rejected or Claude rejected
            if not decision.approved:
                return
        else:
            # Pure LTF scalp — bypass Claude entirely
            logger.info(f"AI SKIPPED: scalping LTF-only ({setup.direction}/{htf_bias}) — bypassing Claude")
            _persist_ai_skip(setup)
            if _notifier is not None:
                await _notifier.notify_ai_skipped(setup)
    elif _ai_service is not None and _data_service is not None:
        # Default / Aggressive — always go through pre-filter + Claude
        decision = await _evaluate_with_claude(setup, candle)
        if decision is None:
            return  # pre-filter rejected or Claude rejected
        if not decision.approved:
            return

    # Layer 4: Risk Service — enforce guardrails + position sizing
    approval = None
    if _risk_service is not None:
        approval = _risk_service.check(setup)
        if not approval.approved:
            logger.info(f"Risk rejected: {approval.reason}")
            _persist_risk_event("trade_rejected", {
                "pair": setup.pair,
                "direction": setup.direction,
                "reason": approval.reason,
            })
            # Telegram: risk rejected
            if _notifier is not None:
                await _notifier.notify_risk_rejected(setup, approval.reason)
            return
        logger.info(
            f"Risk approved: size={approval.position_size:.6f} "
            f"leverage={approval.leverage:.2f}x risk={approval.risk_pct*100:.1f}%"
        )

    # Layer 5: Execution Service — place orders on exchange
    if _execution_service is not None and approval is not None and approval.approved:
        ai_confidence = decision.confidence if decision else 0.0
        await _execution_service.execute(setup, approval, ai_confidence)


# ================================================================
# Persistence helpers (called from pipeline callback)
# ================================================================

def _persist_ai_decision(trade_id, decision, setup) -> None:
    """Write AI decision to PostgreSQL (fire-and-forget)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_ai_decision(
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


async def _evaluate_with_claude(setup, candle) -> "AIDecision | None":
    """Run pre-filter then Claude evaluation. Returns AIDecision or None if rejected/failed."""
    if _ai_service is None or _data_service is None:
        return None

    snapshot = _data_service.get_market_snapshot(candle.pair)

    # Pre-filter: reject obvious losers before calling Claude
    reject_reason = _pre_filter_for_claude(setup, snapshot)
    if reject_reason:
        logger.info(f"AI PRE-FILTERED: {reject_reason}")
        _persist_ai_pre_filter(setup, reject_reason)
        if _notifier is not None:
            await _notifier.notify_ai_pre_filtered(setup, reject_reason)
        return None

    # Claude evaluation
    decision = await _ai_service.evaluate(setup, snapshot)
    _persist_ai_decision(None, decision, setup)
    if _notifier is not None:
        await _notifier.notify_ai_decision(setup, decision)

    if not decision.approved:
        logger.info(
            f"AI rejected: confidence={decision.confidence:.2f} "
            f"reason={decision.reasoning}"
        )
    else:
        logger.info(f"AI approved: confidence={decision.confidence:.2f}")

    return decision


def _pre_filter_for_claude(setup, snapshot) -> str | None:
    """Deterministic pre-filter before Claude API call.

    Returns rejection reason string if setup should be rejected, None if it should
    proceed to Claude. Conservative: skips checks when data is unavailable.
    """
    threshold = settings.FUNDING_EXTREME_THRESHOLD

    # Check 1: Funding rate extreme against trade direction
    if snapshot.funding is not None and snapshot.funding.current_rate is not None:
        rate = snapshot.funding.current_rate
        if setup.direction == "long" and rate > threshold:
            return f"Funding extreme against long ({rate*100:.4f}% > {threshold*100:.4f}%)"
        if setup.direction == "short" and rate < -threshold:
            return f"Funding extreme against short ({rate*100:.4f}% < -{threshold*100:.4f}%)"

    # Check 2: CVD strong divergence against trade direction
    if snapshot.cvd is not None:
        buy_vol = snapshot.cvd.buy_volume_5m
        sell_vol = snapshot.cvd.sell_volume_5m
        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            buy_dominance = buy_vol / total_vol
            if setup.direction == "long" and buy_dominance < 0.40:
                return f"CVD divergence against long (buy dominance {buy_dominance*100:.1f}% < 40%)"
            if setup.direction == "short" and buy_dominance > 0.60:
                return f"CVD divergence against short (buy dominance {buy_dominance*100:.1f}% > 60%)"

    return None


def _persist_ai_pre_filter(setup, reason: str) -> None:
    """Write synthetic AI decision for pre-filter rejection (audit trail)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_ai_decision(
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


def _persist_ai_skip(setup) -> None:
    """Write synthetic AI decision for scalping bypass (audit trail)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_ai_decision(
            trade_id=None,
            confidence=0.0,
            reasoning="Scalping profile — AI filter bypassed",
            adjustments=None,
            warnings=[],
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            approved=True,
        )
    except Exception as e:
        logger.error(f"Failed to persist AI skip: {e}")


def _persist_risk_event(event_type: str, details: dict) -> None:
    """Write risk event to PostgreSQL (fire-and-forget)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_risk_event(event_type, details)
    except Exception as e:
        logger.error(f"Failed to persist risk event: {e}")


# ================================================================
# Config validation
# ================================================================

def validate_config() -> bool:
    """Check that minimum required config is present."""
    ok = True

    if not settings.OKX_API_KEY:
        logger.warning("OKX_API_KEY not set — trading will be disabled (market data still works)")

    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — AI filter disabled, trades will be auto-rejected")

    if settings.OKX_SANDBOX:
        logger.info("Running in DEMO mode (OKX sandbox / simulated trading)")
    else:
        logger.info("Running in LIVE mode (OKX mainnet)")

    logger.info(f"Trading pairs: {settings.TRADING_PAIRS}")
    logger.info(f"Timeframes: HTF={settings.HTF_TIMEFRAMES} LTF={settings.LTF_TIMEFRAMES}")
    logger.info(f"Risk per trade: {settings.RISK_PER_TRADE*100:.1f}%")
    logger.info(f"Max leverage: {settings.MAX_LEVERAGE}x")
    logger.info(f"Max daily DD: {settings.MAX_DAILY_DRAWDOWN*100:.1f}%")

    return ok


# ================================================================
# Main
# ================================================================

async def main() -> None:
    logger.info("=" * 60)
    logger.info("ONE-MAN QUANT FUND — Starting")
    logger.info("=" * 60)

    if not validate_config():
        logger.error("Config validation failed. Exiting.")
        sys.exit(1)

    global _data_service, _strategy_service, _ai_service, _risk_service, _execution_service, _notifier

    # Create Telegram notifier (disabled gracefully if not configured)
    _notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)

    # Create DataService with pipeline callback + notifier for whale alerts
    _data_service = DataService(on_candle_confirmed=on_candle_confirmed, notifier=_notifier)

    # Create StrategyService — Layer 2
    _strategy_service = StrategyService(_data_service)
    logger.info("Strategy Service initialized")

    # Create AIService — Layer 3
    _ai_service = AIService(_data_service)

    # Create RiskService — Layer 4 ($100 demo capital)
    _risk_service = RiskService(capital=100.0, data_service=_data_service)

    # Create ExecutionService — Layer 5
    _execution_service = ExecutionService(_risk_service, _data_service, notifier=_notifier)
    await _execution_service.start()

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info(f"Received {sig_name} — initiating graceful shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start DataService in background
    data_task = asyncio.create_task(_data_service.start(), name="data_service")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    if _execution_service is not None:
        await _execution_service.stop()
    if _ai_service is not None:
        await _ai_service.close()
    await _data_service.stop()

    # Cancel main task if still running
    if not data_task.done():
        data_task.cancel()
        try:
            await data_task
        except asyncio.CancelledError:
            pass

    logger.info("=" * 60)
    logger.info("ONE-MAN QUANT FUND — Stopped")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
