"""Tests for the engine1 ML-score LIVE gate routing (Phase 2).

Proves the decision logic in `_process_pipeline_setup`:
  - flag ON  + score >= cutoff + no kill -> REAL execution (engine1 bypasses AI,
    risk.check gets risk_usd=ENGINE1_RISK_USD)
  - flag ON  + score <  cutoff          -> shadow (unchanged)
  - flag OFF + score >= cutoff          -> shadow (unchanged)
  - flag ON  + score >= cutoff + KILL   -> reverts to shadow + no execution

Scoring + kill-check are patched (returned values controlled) so these tests
need neither the frozen model nor a database.
"""
import asyncio
import sys
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from shared.models import (
    Candle, TradeSetup, RiskApproval,
    MarketSnapshot, FundingRate, CVDSnapshot, SnapshotHealth,
)
from data_service.data_integrity import DataServiceState, CVDState
from config.settings import settings


def _noop_logger(name=""):
    from loguru import logger
    return logger


if "main" in sys.modules:
    del sys.modules["main"]
with patch("shared.logger.setup_logger", side_effect=_noop_logger):
    if "shared.notifier" in sys.modules:
        del sys.modules["shared.notifier"]
    import main
    from pipeline_runtime import rt

CUTOFF = settings.ENGINE1_SCORE_CUTOFF


def _make_candle(pair="ETH/USDT", close=2000.0) -> Candle:
    return Candle(
        timestamp=int(time.time() * 1000),
        open=close - 50, high=close + 50, low=close - 100,
        close=close, volume=1.0, volume_quote=close,
        pair=pair, timeframe="5m", confirmed=True,
    )


def _make_engine1_setup(pair="ETH/USDT") -> TradeSetup:
    # engine1 is short-only and quarantined to ETH/SOL/LINK/AVAX/XRP.
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair, direction="short", setup_type="engine1_trend_pullback",
        entry_price=2000.0, sl_price=2040.0,
        tp1_price=1960.0, tp2_price=1920.0,
        confluences=["trend", "pullback"],
        htf_bias="bearish", ob_timeframe="1h",
    )


def _make_snapshot(pair="ETH/USDT") -> MarketSnapshot:
    ts = int(time.time() * 1000)
    snap = MarketSnapshot(
        pair=pair, timestamp=ts,
        funding=FundingRate(
            timestamp=ts, pair=pair, rate=0.0001,
            next_rate=0.0001, next_funding_time=ts + 28800000,
        ),
        cvd=CVDSnapshot(
            timestamp=ts, pair=pair,
            cvd_5m=10.0, cvd_15m=30.0, cvd_1h=100.0,
            buy_volume=500.0, sell_volume=400.0,
        ),
    )
    snap.health = SnapshotHealth(
        sources=(), completeness_pct=1.0, critical_sources_healthy=True,
        stale_sources=(), missing_sources=(),
    )
    return snap


def _wire(approval_ok=True):
    """Wire mock services for the engine1 live-gate path."""
    data = MagicMock()
    data.state = DataServiceState.RUNNING
    data.get_cvd_state.return_value = CVDState.VALID
    data.get_market_snapshot.return_value = _make_snapshot()
    data.get_orderbook_snapshot.return_value = None
    data.postgres = MagicMock()
    rt.data_service = data

    strategy = MagicMock()
    strategy.is_ob_failed.return_value = False
    rt.strategy_service = strategy

    risk = MagicMock()
    risk._state.get_capital.return_value = 100.0
    risk.check.return_value = RiskApproval(
        approved=approval_ok, position_size=0.02 if approval_ok else 0.0,
        leverage=1.0, risk_pct=0.015, reason="" if approval_ok else "rejected",
    )
    rt.risk_service = risk

    execution = AsyncMock()
    execution.execute.return_value = True
    rt.execution_service = execution

    shadow = MagicMock()
    shadow.add_shadow.return_value = True
    rt.shadow_monitor = shadow

    rt.ai_service = None
    rt.alert_manager = None
    rt.notifier = None
    return data, risk, execution, shadow


@pytest.fixture(autouse=True)
def _reset():
    rt.setup_dedup_cache.clear()
    for attr in ("_data_service", "_strategy_service", "_ai_service",
                 "_risk_service", "_execution_service", "_shadow_monitor",
                 "_alert_manager", "_notifier"):
        setattr(main, attr, None)
    yield
    rt.setup_dedup_cache.clear()
    for attr in ("_data_service", "_strategy_service", "_ai_service",
                 "_risk_service", "_execution_service", "_shadow_monitor",
                 "_alert_manager", "_notifier"):
        setattr(main, attr, None)


def _run(setup, score, *, flag, kill=(False, None), approval_ok=True):
    """Drive _process_pipeline_setup with controlled score + kill verdict."""
    data, risk, execution, shadow = _wire(approval_ok=approval_ok)
    with patch.object(main, "_ml_log_setup", return_value={"f": 1}), \
         patch.object(main, "_engine1_score_log", return_value=score), \
         patch.object(main, "_engine1_kill_check", return_value=kill), \
         patch.object(settings, "ENGINE1_LIVE_GATED_ENABLED", flag), \
         patch.object(settings, "MIN_ORDER_SIZES", {}):
        asyncio.run(main._process_pipeline_setup(setup, _make_candle(), allow_live=True))
    return risk, execution, shadow


def test_flag_on_high_score_routes_to_execution():
    setup = _make_engine1_setup()
    risk, execution, shadow = _run(setup, CUTOFF + 0.05, flag=True)
    execution.execute.assert_called_once()
    shadow.add_shadow.assert_not_called()
    # engine1 live trade sizes against ENGINE1_RISK_USD.
    _, kwargs = risk.check.call_args
    assert kwargs.get("risk_usd") == settings.ENGINE1_RISK_USD


def test_flag_on_low_score_stays_shadow():
    setup = _make_engine1_setup()
    _, execution, shadow = _run(setup, CUTOFF - 0.05, flag=True)
    execution.execute.assert_not_called()
    shadow.add_shadow.assert_called_once()


def test_flag_off_high_score_stays_shadow():
    setup = _make_engine1_setup()
    _, execution, shadow = _run(setup, CUTOFF + 0.05, flag=False)
    execution.execute.assert_not_called()
    shadow.add_shadow.assert_called_once()


def test_kill_switch_reverts_high_score_to_shadow():
    setup = _make_engine1_setup()
    _, execution, shadow = _run(
        setup, CUTOFF + 0.05, flag=True, kill=(True, "11.0R drawdown"),
    )
    execution.execute.assert_not_called()
    shadow.add_shadow.assert_called_once()


def test_non_engine1_setup_unaffected_by_flag():
    """A non-engine1 shadow setup never touches the engine1 live path."""
    setup = _make_engine1_setup()
    object.__setattr__(setup, "setup_type", "bench_engine1_market_now")
    _, execution, shadow = _run(setup, CUTOFF + 0.05, flag=True)
    execution.execute.assert_not_called()
    shadow.add_shadow.assert_called_once()
