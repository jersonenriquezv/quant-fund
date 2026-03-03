"""
Entry point for the One-Man Quant Fund trading bot.

Single Python process running all 5 layers:
    Data Service → Strategy Service → AI Service → Risk Service → Execution Service

Current state: Data Service + Strategy Service. AI/Risk/Execution are stubs
that will be wired in as they're built.

Usage:
    python main.py
"""

import asyncio
import signal
import sys

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle
from data_service.service import DataService
from strategy_service import StrategyService
from risk_service import RiskService

logger = setup_logger("main")

# Module-level references set by main() so the callback can access them
_strategy_service: StrategyService | None = None
_risk_service: RiskService | None = None


# ================================================================
# Pipeline callback — triggered on every confirmed candle
# ================================================================

async def on_candle_confirmed(candle: Candle) -> None:
    """Pipeline entry point: Data → Strategy → AI → Risk → Execution.

    Strategy Service evaluates LTF candles for SMC setups.
    AI/Risk/Execution layers are stubs — will be wired as they're built.
    """
    logger.info(
        f"Pipeline triggered: pair={candle.pair} tf={candle.timeframe} "
        f"close={candle.close} vol={candle.volume:.4f}"
    )

    # Layer 2: Strategy Service — detect SMC setups
    if _strategy_service is None:
        return

    setup = _strategy_service.evaluate(candle.pair, candle)
    if setup is None:
        return

    logger.info(
        f"Trade setup detected: type={setup.setup_type} pair={setup.pair} "
        f"direction={setup.direction} entry={setup.entry_price:.2f} "
        f"sl={setup.sl_price:.2f} tp1={setup.tp1_price:.2f} "
        f"confluences={setup.confluences}"
    )

    # TODO: Wire AI Service here
    # snapshot = data_service.get_market_snapshot(candle.pair)
    # decision = ai_service.evaluate(setup, snapshot)
    # if not decision.approved:
    #     return

    # Layer 4: Risk Service — enforce guardrails + position sizing
    if _risk_service is not None:
        approval = _risk_service.check(setup)
        if not approval.approved:
            logger.info(f"Risk rejected: {approval.reason}")
            return
        logger.info(
            f"Risk approved: size={approval.position_size:.6f} "
            f"leverage={approval.leverage:.2f}x risk={approval.risk_pct*100:.1f}%"
        )

    # TODO: Wire Execution Service here
    # execution_service.execute(setup, approval)


# ================================================================
# Config validation
# ================================================================

def validate_config() -> bool:
    """Check that minimum required config is present."""
    ok = True

    if not settings.OKX_API_KEY:
        logger.warning("OKX_API_KEY not set — trading will be disabled (market data still works)")

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

    global _strategy_service, _risk_service

    # Create DataService with pipeline callback
    data_service = DataService(on_candle_confirmed=on_candle_confirmed)

    # Create StrategyService — Layer 2
    _strategy_service = StrategyService(data_service)
    logger.info("Strategy Service initialized")

    # Create RiskService — Layer 4 ($100 demo capital)
    _risk_service = RiskService(capital=100.0)
    logger.info("Risk Service initialized")

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info(f"Received {sig_name} — initiating graceful shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start DataService in background
    data_task = asyncio.create_task(data_service.start(), name="data_service")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    await data_service.stop()

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
