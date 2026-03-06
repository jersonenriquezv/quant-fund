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
import time

from config.settings import settings, STRATEGY_PROFILES, QUICK_SETUP_TYPES, apply_profile, reset_profile
from shared.logger import setup_logger
from shared.models import Candle, AIDecision
from data_service.service import DataService
from strategy_service import StrategyService
from ai_service import AIService
from risk_service import RiskService
from execution_service import ExecutionService
from shared.notifier import TelegramNotifier

logger = setup_logger("main")

# Setup deduplication cache — prevents sending the same setup to Claude every 5m candle.
# Key: (pair, direction, setup_type, entry_price), Value: unix timestamp of last eval.
_setup_dedup_cache: dict[tuple, float] = {}
_SETUP_DEDUP_TTL_SECONDS = 3600  # 1 hour — prevents re-sending same setup while limit order is pending

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

    # Send OB summary on every 4H candle close
    if candle.timeframe == "4h" and _notifier is not None:
        obs = _strategy_service.get_active_order_blocks(candle.pair)
        htf_bias = _strategy_service.get_htf_bias(candle.pair)
        await _notifier.notify_ob_summary(candle.pair, obs, htf_bias, candle.close)

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

    # Layer 3: AI Service — Claude filter
    # Quick setups (C/D/E) bypass Claude — the data IS the signal
    decision = None
    if setup.setup_type in QUICK_SETUP_TYPES:
        decision = AIDecision(
            confidence=1.0,
            approved=True,
            reasoning=f"Data-driven quick setup ({setup.setup_type}) — AI bypass",
            adjustments={},
            warnings=[],
        )
        logger.info(f"AI bypass: {setup.setup_type} — data-driven quick setup")
    elif _ai_service is not None and _data_service is not None:
        decision = await _evaluate_with_claude(setup, candle)
        if decision is None:
            return  # pre-filter rejected or Claude failed
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

    # Dedup: don't re-send the same setup to Claude within TTL
    dedup_key = (setup.pair, setup.direction, setup.setup_type,
                 round(setup.entry_price, 2))
    now = time.time()
    last_eval = _setup_dedup_cache.get(dedup_key)
    if last_eval and (now - last_eval) < _SETUP_DEDUP_TTL_SECONDS:
        logger.debug(
            f"Setup dedup: {setup.pair} {setup.direction} {setup.setup_type} "
            f"entry={setup.entry_price:.2f} — already evaluated "
            f"{int(now - last_eval)}s ago, skipping Claude"
        )
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
    _setup_dedup_cache[dedup_key] = time.time()
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

    Setup C skips funding check (extreme funding IS the signal).
    """
    # HTF bias is no longer a hard gate — Claude evaluates it as context.
    # LTF structure (CHoCH/BOS) drives trade direction; strong counter-trend
    # setups with clear LTF structure can proceed to Claude for evaluation.

    threshold = settings.FUNDING_EXTREME_THRESHOLD

    # Check 1: Funding rate extreme against trade direction
    # Skip for Setup C — extreme funding IS the signal
    if setup.setup_type != "setup_c":
        if snapshot.funding is not None and snapshot.funding.rate is not None:
            rate = snapshot.funding.rate
            if setup.direction == "long" and rate > threshold:
                return f"Funding extreme against long ({rate*100:.4f}% > {threshold*100:.4f}%)"
            if setup.direction == "short" and rate < -threshold:
                return f"Funding extreme against short ({rate*100:.4f}% < -{threshold*100:.4f}%)"

    # Check 2: CVD strong divergence against trade direction
    if snapshot.cvd is not None:
        buy_vol = snapshot.cvd.buy_volume
        sell_vol = snapshot.cvd.sell_volume
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



def _persist_risk_event(event_type: str, details: dict) -> None:
    """Write risk event to PostgreSQL (fire-and-forget)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_risk_event(event_type, details)
    except Exception as e:
        logger.error(f"Failed to persist risk event: {e}")


# ================================================================
# Hourly status loop
# ================================================================

_bot_start_time: float = 0.0


async def _hourly_status_loop() -> None:
    """Send hourly status to Telegram."""
    while True:
        await asyncio.sleep(3600)  # Every hour
        if _notifier is None:
            continue
        try:
            # Uptime
            elapsed = int(time.time() - _bot_start_time)
            hours, remainder = divmod(elapsed, 3600)
            minutes = remainder // 60
            uptime_str = f"{hours}h {minutes}m"

            # Prices
            prices: dict[str, float] = {}
            for pair in settings.TRADING_PAIRS:
                if _data_service is not None:
                    candle = _data_service.get_latest_candle(pair, "5m")
                    if candle is not None:
                        prices[pair] = candle.close

            # HTF bias
            htf_bias: dict[str, str] = {}
            if _strategy_service is not None:
                for pair in settings.TRADING_PAIRS:
                    htf_bias[pair] = _strategy_service.get_htf_bias(pair)

            # Risk state
            open_positions = 0
            trades_today = 0
            daily_dd = 0.0
            weekly_dd = 0.0
            if _risk_service is not None:
                tracker = _risk_service._state
                open_positions = tracker.get_open_positions_count()
                trades_today = tracker.get_trades_today_count()
                daily_dd = tracker.get_daily_dd_pct()
                weekly_dd = tracker.get_weekly_dd_pct()

            await _notifier.notify_hourly_status(
                uptime_str=uptime_str,
                profile=settings.STRATEGY_PROFILE,
                open_positions=open_positions,
                trades_today=trades_today,
                daily_dd_pct=daily_dd,
                weekly_dd_pct=weekly_dd,
                prices=prices,
                htf_bias=htf_bias,
            )
        except Exception as e:
            logger.error(f"Hourly status failed: {e}")


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

    # Create RiskService — Layer 4 (capital from exchange balance or INITIAL_CAPITAL fallback)
    balance = _data_service.fetch_usdt_balance()
    if balance is not None and balance > 0:
        capital = balance
        logger.info(f"Capital from exchange: ${capital:.2f}")
    else:
        capital = settings.INITIAL_CAPITAL
        logger.warning(f"Could not fetch balance — using INITIAL_CAPITAL: ${capital:.2f}")
    _risk_service = RiskService(capital=capital, data_service=_data_service)

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

    # Start hourly status loop
    global _bot_start_time
    _bot_start_time = time.time()
    status_task = asyncio.create_task(_hourly_status_loop(), name="hourly_status")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    if _execution_service is not None:
        await _execution_service.stop()
    if _ai_service is not None:
        await _ai_service.close()
    await _data_service.stop()

    # Cancel background tasks
    for task in [data_task, status_task]:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    logger.info("=" * 60)
    logger.info("ONE-MAN QUANT FUND — Stopped")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
