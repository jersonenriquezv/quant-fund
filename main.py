"""
Entry point for the One-Man Quant Fund trading bot.

Single Python process running all 5 layers:
    Data Service → Strategy Service → AI Service → Risk Service → Execution Service

Runs in Docker via docker-compose.yml (bot + PostgreSQL + Redis).

Usage:
    python main.py
"""

import asyncio
import os
import signal
import sys
import time

from config.settings import settings
from shared.logger import setup_logger
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
from telegram_bot import TelegramInteractiveBot
from pipeline_runtime import rt
from monitoring_loops import (
    _session_alert_loop,
    _dry_spell_loop,
    _market_monitor_loop,
    _liquidation_alert_loop,
)
# Pipeline core + ML instrumentation extracted to their own modules
# (Refactor Phase 6). main() wires on_candle_confirmed into DataService; the
# re-exported helpers keep `main.<fn>` references resolving for tests.
from pipeline_router import (
    on_candle_confirmed,
    _process_pipeline_setup,
    _pre_filter_for_claude,
)
from ml_instrumentation import _engine1_score_log

logger = setup_logger("main")

# ================================================================
# Startup pair diagnostic
# ================================================================

def _log_pair_diagnostics(capital: float, postgres) -> None:
    """Log whether each pair can meet minimum order size at current capital.

    Shows both live (real balance) and shadow (virtual capital) viability.
    Uses last candle close from PostgreSQL (no REST call needed).
    """
    risk_pct = settings.RISK_PER_TRADE
    live_risk = capital * risk_pct
    shadow_capital = settings.effective_shadow_capital
    shadow_risk = shadow_capital * risk_pct
    typical_sl_pct = 0.01

    logger.info(
        f"--- Pair Diagnostic | LIVE: ${capital:.2f} | "
        f"SHADOW: ${shadow_capital:.0f} virtual | "
        f"risk={risk_pct*100:.1f}% ---"
    )

    live_pairs = []
    shadow_pairs = []

    for pair in settings.TRADING_PAIRS:
        min_size = settings.MIN_ORDER_SIZES.get(pair, 0)

        # Get last known price from DB
        last_price = 0.0
        if postgres is not None:
            try:
                candles = postgres.load_candles(pair, "15m", count=1)
                if candles:
                    last_price = candles[-1].close
            except Exception:
                pass

        if last_price <= 0:
            logger.warning(f"  {pair}: no price data — cannot compute position size")
            continue

        sl_distance = last_price * typical_sl_pct

        # Live sizing
        live_size = live_risk / sl_distance if sl_distance > 0 else 0
        live_ok = live_size >= min_size

        # Shadow sizing
        shadow_size = shadow_risk / sl_distance if sl_distance > 0 else 0
        shadow_ok = shadow_size >= min_size

        if live_ok:
            live_pairs.append(pair)
        if shadow_ok:
            shadow_pairs.append(pair)

        mode = "LIVE" if live_ok else ("SHADOW-ONLY" if shadow_ok else "BLOCKED")
        logger.info(
            f"  {pair}: ${last_price:.2f} | "
            f"live={live_size:.6f} shadow={shadow_size:.6f} "
            f"(min={min_size}) [{mode}]"
        )

        if not live_ok and not shadow_ok:
            needed = (min_size * sl_distance) / risk_pct
            logger.warning(f"  {pair}: need ${needed:.0f} even for shadow")

    # Summary
    live_short = [p.replace("/USDT", "") for p in live_pairs]
    shadow_short = [p.replace("/USDT", "") for p in shadow_pairs if p not in live_pairs]
    logger.info(
        f"LIVE-VIABLE: {', '.join(live_short) or 'NONE'} (balance: ${capital:.2f})"
    )
    logger.info(
        f"SHADOW-ONLY: {', '.join(shadow_short) or 'NONE'} "
        f"(${shadow_capital:.0f} virtual)"
    )
    logger.info("--- End Pair Diagnostic ---")


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

    if settings.SIGNAL_ONLY:
        logger.info("SIGNAL MODE — signals only, no automatic execution")

    if settings.OKX_SANDBOX:
        logger.info("Running in DEMO mode (OKX sandbox / simulated trading)")
    else:
        logger.info("Running in LIVE mode (OKX mainnet)")

    logger.info(f"Trading pairs: {settings.TRADING_PAIRS}")
    logger.info(f"Timeframes: HTF={settings.HTF_TIMEFRAMES} LTF={settings.LTF_TIMEFRAMES}")
    logger.info(f"Risk per trade: {settings.RISK_PER_TRADE*100:.1f}%")
    logger.info(f"Max leverage: {settings.MAX_LEVERAGE}x")
    logger.info(f"Max daily DD: {settings.MAX_DAILY_DRAWDOWN*100:.1f}%")

    # ML data-collection identity — every ml_setups row written this session
    # is tagged with these two values. Log them prominently so dashboards,
    # training queries, and post-hoc analysis can reconstruct the regime.
    env_exp = os.getenv("EXPERIMENT_ID")
    exp_source = "env override" if env_exp else "settings default"
    env_scalp_exp = os.getenv("SCALP_EXPERIMENT_ID")
    scalp_exp_source = "env override" if env_scalp_exp else "settings default"
    logger.info(
        f"ML tagging: feature_version={settings.ML_FEATURE_VERSION} "
        f"experiment_id='{settings.EXPERIMENT_ID}' ({exp_source}) "
        f"scalp_experiment_id='{settings.SCALP_EXPERIMENT_ID}' ({scalp_exp_source})"
    )

    if settings.HTF_CAMPAIGN_ENABLED:
        logger.info(
            f"HTF campaigns ENABLED: signal={settings.HTF_CAMPAIGN_SIGNAL_TF} "
            f"bias={settings.HTF_CAMPAIGN_BIAS_TF} "
            f"initial_margin=${settings.HTF_INITIAL_MARGIN} "
            f"max_adds={settings.HTF_MAX_ADDS}"
        )

    if settings.SHADOW_MODE_SETUPS:
        live_setups = [s for s in settings.ENABLED_SETUPS if s not in settings.SHADOW_MODE_SETUPS]
        logger.info(
            f"SHADOW: {settings.SHADOW_MODE_SETUPS} "
            f"(${settings.effective_shadow_capital} virtual, basis={settings.SHADOW_CAPITAL_BASIS})"
        )
        logger.info(f"LIVE: {live_setups}")

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


    # Create Telegram notifier + AlertManager wrapper
    rt.notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    rt.alert_manager = AlertManager(rt.notifier, enabled=settings.BOT_TELEGRAM_ALERTS_ENABLED)
    if not settings.BOT_TELEGRAM_ALERTS_ENABLED:
        logger.info(
            "Bot Telegram alerts MUTED (BOT_TELEGRAM_ALERTS_ENABLED=false): "
            "only crash/error + live trade lifecycle reach Telegram. "
            "Routine shadow/session/market noise suppressed. Daily digest via "
            "scripts/daily_status.py."
        )

    # Create DataService with pipeline callback + alert manager for whale alerts
    rt.data_service = DataService(on_candle_confirmed=on_candle_confirmed, alert_manager=rt.alert_manager)

    # Create StrategyService — Layer 2
    rt.strategy_service = StrategyService(rt.data_service)
    logger.info("Strategy Service initialized")

    # Create AIService — Layer 3
    rt.ai_service = AIService(rt.data_service)

    # Create RiskService — Layer 4 (capital from exchange balance or INITIAL_CAPITAL fallback)
    balance = rt.data_service.fetch_usdt_balance()
    if balance is not None and balance > 0:
        capital = balance
        logger.info(f"Capital from exchange: ${capital:.2f}")
    else:
        capital = settings.INITIAL_CAPITAL
        logger.warning(f"Could not fetch balance — using INITIAL_CAPITAL: ${capital:.2f}")
    rt.risk_service = RiskService(capital=capital, data_service=rt.data_service)

    # Startup pair diagnostic — check capital vs min order requirements
    _log_pair_diagnostics(capital, rt.data_service.postgres)

    # Reconcile drawdown from PostgreSQL (source of truth for realized PnL).
    # Catches cases where Redis state was lost or stale after restart.
    rt.risk_service._state.reconcile_drawdown_from_db(rt.data_service.postgres)

    # Create ExecutionService — Layer 5
    # on_sl_hit callback marks failed OBs so the same OB doesn't re-trigger
    def _on_sl_hit(pair: str, sl_price: float, entry_price: float) -> None:
        if rt.strategy_service is not None:
            rt.strategy_service.mark_ob_failed(pair, sl_price, entry_price)

    rt.execution_service = ExecutionService(
        rt.risk_service, rt.data_service, alert_manager=rt.alert_manager,
        on_sl_hit=_on_sl_hit
    )
    await rt.execution_service.start()

    # Create ShadowMonitor for theoretical outcome tracking. When bot alerts
    # are muted, pass no notifier so shadow tracking/fill/resolution stays
    # silent — outcome tracking (ML data) continues unchanged either way.
    if settings.SHADOW_MODE_SETUPS:
        _shadow_notifier = rt.notifier if settings.BOT_TELEGRAM_ALERTS_ENABLED else None
        rt.shadow_monitor = ShadowMonitor(rt.data_service, notifier=_shadow_notifier)
        logger.info(
            f"Shadow Monitor initialized: {settings.SHADOW_MODE_SETUPS} "
            f"(${settings.effective_shadow_capital} virtual, basis={settings.SHADOW_CAPITAL_BASIS})"
        )

    # Dual Thrust shadow tracker (order-free, ETH 4h). Reads authoritative OKX
    # REST 4h bars via the exchange client (forming bar already dropped post the
    # partial-candle fix). docs/plans/dual-thrust-phase1b-shadow-wiring.md Phase 2.
    if settings.DUAL_THRUST_SHADOW_ENABLED:
        rt.dual_thrust_shadow = DualThrustShadowTracker(
            candle_fetcher=lambda: rt.data_service._exchange.backfill_candles(
                "ETH/USDT", "4h", 500)
        )
        logger.info("Dual Thrust shadow tracker initialized (ETH/USDT 4h, order-free)")

    # Create CampaignMonitor for HTF position trades (when enabled)
    if settings.HTF_CAMPAIGN_ENABLED and rt.execution_service._executor is not None:
        rt.campaign_monitor = CampaignMonitor(
            executor=rt.execution_service._executor,
            risk_service=rt.risk_service,
            strategy_service=rt.strategy_service,
            data_store=rt.data_service,
            alert_manager=rt.alert_manager,
        )
        rt.campaign_monitor.start()
        logger.info("HTF Campaign Monitor started")
    elif settings.HTF_CAMPAIGN_ENABLED:
        logger.warning("HTF campaigns enabled but execution disabled (no OKX key)")

    # Start interactive Telegram bot (inline keyboard menus)
    _interactive_bot = None
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        try:
            _interactive_bot = TelegramInteractiveBot(
                token=settings.TELEGRAM_BOT_TOKEN,
                allowed_chat_ids={int(settings.TELEGRAM_CHAT_ID)},
                data_service=rt.data_service,
                strategy_service=rt.strategy_service,
                risk_service=rt.risk_service,
                execution_service=rt.execution_service,
                shadow_monitor=rt.shadow_monitor,
                bot_start_time=time.time(),
                get_last_setup_time=lambda: rt.last_setup_detected_time,
            )
            await _interactive_bot.start()
            logger.info("Telegram interactive bot started")
        except Exception as e:
            logger.warning(f"Telegram interactive bot failed to start: {e}")
            _interactive_bot = None

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info(f"Received {sig_name} — initiating graceful shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start DataService in background
    data_task = asyncio.create_task(rt.data_service.start(), name="data_service")

    # Start background monitoring loops
    rt.bot_start_time = time.time()
    liq_task = asyncio.create_task(_liquidation_alert_loop(), name="liquidation_alerts")
    session_task = asyncio.create_task(_session_alert_loop(), name="session_alerts")
    dry_spell_task = asyncio.create_task(_dry_spell_loop(), name="dry_spell_alerts")
    market_monitor_task = asyncio.create_task(_market_monitor_loop(), name="market_monitor")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    if _interactive_bot is not None:
        await _interactive_bot.stop()
    if rt.campaign_monitor is not None:
        await rt.campaign_monitor.stop()
    if rt.execution_service is not None:
        await rt.execution_service.stop()
    if rt.ai_service is not None:
        await rt.ai_service.close()
    await rt.data_service.stop()

    # Cancel background tasks
    for task in [data_task, liq_task, session_task, dry_spell_task, market_monitor_task]:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    logger.info("=" * 60)
    logger.info("ONE-MAN QUANT FUND — Stopped")
    logger.info("=" * 60)


def _send_crash_alert(exc: BaseException) -> None:
    """Real-time Telegram alert on unhandled crash. Bypasses the AlertManager
    mute (own notifier, direct send) so a process-down event always reaches the
    phone even when routine alerts are disabled."""
    try:
        import traceback
        notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
        tail = traceback.format_exc().strip().splitlines()[-1][:300]
        msg = (
            f"\U0001f6a8 <b>BOT CRASHED</b>\n"
            f"<code>{type(exc).__name__}: {str(exc)[:300]}</code>\n"
            f"{tail}\n"
            f"Process is down — Docker will attempt restart. Check "
            f"<code>docker compose logs bot --tail=80</code>."
        )
        asyncio.run(notifier.send(msg))
    except Exception:
        logger.critical("crash alert failed to send", exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.critical(f"BOT CRASHED (unhandled): {exc}", exc_info=True)
        _send_crash_alert(exc)
        raise
