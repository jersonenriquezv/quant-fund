"""Tests for main.py — pipeline wiring and pre-filter logic.

Mocks all 5 services to test the on_candle_confirmed callback
and _pre_filter_for_claude / _evaluate_with_claude logic.

Note: main.py import triggers logger file creation (owned by Docker/root).
We patch setup_logger before importing to avoid PermissionError.
"""

import time
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from shared.models import (
    Candle, TradeSetup, AIDecision, RiskApproval,
    MarketSnapshot, FundingRate, CVDSnapshot, SnapshotHealth,
)
from data_service.data_integrity import DataServiceState, CVDState


# Patch setup_logger to return a no-op logger before importing main.
# This avoids loguru trying to open root-owned log files in tests.
def _noop_logger(name=""):
    from loguru import logger
    return logger


# Remove main from cache if previously imported, then import with patched logger
if "main" in sys.modules:
    del sys.modules["main"]

with patch("shared.logger.setup_logger", side_effect=_noop_logger):
    # Need to reload notifier too since it calls setup_logger at module level
    if "shared.notifier" in sys.modules:
        del sys.modules["shared.notifier"]
    import main
    from pipeline_runtime import rt


# ============================================================
# Fixtures
# ============================================================

def _make_candle(pair="ETH/USDT", close=2000.0, timeframe="5m") -> Candle:
    return Candle(
        timestamp=int(time.time() * 1000),
        open=close - 50, high=close + 50, low=close - 100,
        close=close, volume=1.0, volume_quote=close,
        pair=pair, timeframe=timeframe, confirmed=True,
    )


def _make_setup(pair="ETH/USDT", direction="long", setup_type="setup_a") -> TradeSetup:
    entry = 2000.0
    sl = 1960.0 if direction == "long" else 2040.0
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair, direction=direction, setup_type=setup_type,
        entry_price=entry, sl_price=sl,
        tp1_price=2040.0 if direction == "long" else 1960.0,
        tp2_price=2080.0 if direction == "long" else 1920.0,
        confluences=["choch", "ob", "sweep"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
    )


def _make_snapshot(
    pair="ETH/USDT",
    funding_rate=0.0001,
    buy_volume=500.0,
    sell_volume=400.0,
) -> MarketSnapshot:
    ts = int(time.time() * 1000)
    return MarketSnapshot(
        pair=pair, timestamp=ts,
        funding=FundingRate(
            timestamp=ts, pair=pair, rate=funding_rate,
            next_rate=funding_rate, next_funding_time=ts + 28800000,
        ),
        cvd=CVDSnapshot(
            timestamp=ts, pair=pair,
            cvd_5m=10.0, cvd_15m=30.0, cvd_1h=100.0,
            buy_volume=buy_volume, sell_volume=sell_volume,
        ),
    )


def _reset_rt():
    rt.setup_dedup_cache.clear()
    rt.data_service = None
    rt.strategy_service = None
    rt.ai_service = None
    rt.risk_service = None
    rt.execution_service = None
    rt.notifier = None
    rt.campaign_monitor = None
    rt.shadow_monitor = None
    rt.dual_thrust_shadow = None
    rt.alert_manager = None
    rt.last_setup_detected_time = 0.0


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset shared runtime (rt) state between tests."""
    _reset_rt()
    yield
    _reset_rt()


def _wire_services(
    setup=None,
    decision=None,
    approval=None,
    snapshot=None,
    htf_bias="bullish",
):
    """Wire mock services into main module globals."""
    strategy = MagicMock()
    strategy.evaluate.return_value = setup
    strategy.evaluate_all.return_value = [setup] if setup is not None else []
    # evaluate_scalp is invoked when settings.SCALP_SHADOW_ENABLED is true
    # (e.g. when a dev .env sets it). Default to None so the pipeline does
    # not append a MagicMock-as-setup to all_setups.
    strategy.evaluate_scalp.return_value = None
    strategy.get_htf_bias.return_value = htf_bias
    strategy.get_active_order_blocks.return_value = []
    strategy.is_ob_failed.return_value = False
    rt.strategy_service = strategy

    data = MagicMock()
    data.state = DataServiceState.RUNNING
    data.get_cvd_state.return_value = CVDState.VALID
    snap = snapshot or _make_snapshot()
    # Attach a healthy SnapshotHealth so can_trade_setup() passes
    if snap.health is None:
        snap.health = SnapshotHealth(
            sources=(),
            completeness_pct=1.0,
            critical_sources_healthy=True,
            stale_sources=(),
            missing_sources=(),
        )
    data.get_market_snapshot.return_value = snap
    data.redis = MagicMock()
    data.redis.get_bot_state.return_value = None
    data.postgres = MagicMock()
    data.postgres.insert_ai_decision.return_value = 1
    rt.data_service = data

    ai = AsyncMock()
    if decision is not None:
        ai.evaluate.return_value = decision
    rt.ai_service = ai

    risk = MagicMock()
    risk._state.get_capital.return_value = 100.0  # $100 test capital
    if approval is not None:
        risk.check.return_value = approval
    rt.risk_service = risk

    execution = AsyncMock()
    execution.execute.return_value = True
    rt.execution_service = execution

    notifier = AsyncMock()
    rt.notifier = notifier

    return strategy, data, ai, risk, execution, notifier


# ============================================================
# Full pipeline — happy path
# ============================================================

class TestPipelineHappyPath:

    def test_setup_approved_and_executed(self):
        """Full pipeline: setup → AI bypass (setup_a) → risk approves → executed."""
        import asyncio
        setup = _make_setup()
        decision = AIDecision(
            confidence=0.80, approved=True,
            reasoning="Strong setup", adjustments={}, warnings=[],
        )
        approval = RiskApproval(
            approved=True, position_size=0.02, leverage=1.0,
            risk_pct=0.02, reason="",
        )
        _, _, ai, risk, execution, _ = _wire_services(
            setup=setup, decision=decision, approval=approval,
        )

        asyncio.run(main.on_candle_confirmed(_make_candle()))

        # setup_a is in AI_BYPASS_SETUP_TYPES — Claude not called
        ai.evaluate.assert_not_called()
        risk.check.assert_called_once_with(setup, ai_confidence=1.0, risk_usd=None)
        execution.execute.assert_called_once()

    def test_no_setup_detected_stops_early(self):
        """No setup → pipeline stops after strategy."""
        import asyncio
        _, _, ai, risk, execution, _ = _wire_services(setup=None)

        asyncio.run(main.on_candle_confirmed(_make_candle()))

        ai.evaluate.assert_not_called()
        risk.check.assert_not_called()
        execution.execute.assert_not_called()


# ============================================================
# AI rejection
# ============================================================

class TestAIRejection:

    def test_ai_rejects_stops_pipeline(self):
        """AI rejects setup → risk and execution never called.

        Uses setup_g (not in QUICK or AI_BYPASS) so it goes through Claude.
        """
        import asyncio
        setup = _make_setup(setup_type="setup_g")
        decision = AIDecision(
            confidence=0.40, approved=False,
            reasoning="CVD divergence", adjustments={}, warnings=[],
        )
        _, _, _, risk, execution, _ = _wire_services(
            setup=setup, decision=decision,
        )

        asyncio.run(main.on_candle_confirmed(_make_candle()))

        risk.check.assert_not_called()
        execution.execute.assert_not_called()


# ============================================================
# Risk rejection
# ============================================================

class TestRiskRejection:

    def test_risk_rejects_stops_execution(self):
        """Risk rejects → execution never called."""
        import asyncio
        setup = _make_setup()
        decision = AIDecision(
            confidence=0.80, approved=True,
            reasoning="Good", adjustments={}, warnings=[],
        )
        approval = RiskApproval(
            approved=False, position_size=0.0, leverage=0.0,
            risk_pct=0.0, reason="Max daily drawdown reached",
        )
        _, _, _, _, execution, _ = _wire_services(
            setup=setup, decision=decision, approval=approval,
        )

        asyncio.run(main.on_candle_confirmed(_make_candle()))

        execution.execute.assert_not_called()


# ============================================================
# Pre-filter
# ============================================================

class TestPreFilter:

    def test_htf_conflict_long_bearish_not_blocked(self):
        """Long + HTF bearish → NOT rejected (HTF is contextual, not a gate)."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot()
        strategy = MagicMock()
        strategy.get_htf_bias.return_value = "bearish"
        rt.strategy_service = strategy

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is None

    def test_htf_conflict_short_bullish_not_blocked(self):
        """Short + HTF bullish → NOT rejected (HTF is contextual, not a gate)."""
        setup = _make_setup(direction="short")
        snapshot = _make_snapshot()
        strategy = MagicMock()
        strategy.get_htf_bias.return_value = "bullish"
        rt.strategy_service = strategy

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is None

    def test_htf_aligned_passes(self):
        """Long + HTF bullish → passes pre-filter."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot()
        strategy = MagicMock()
        strategy.get_htf_bias.return_value = "bullish"
        rt.strategy_service = strategy

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is None

    def test_funding_extreme_long(self):
        """Long + extreme positive funding → rejected."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot(funding_rate=0.001)  # 0.1% >> 0.03%
        rt.strategy_service = None  # skip HTF check

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is not None
        assert "Funding extreme" in reason

    def test_cvd_divergence_long(self):
        """Long + buy dominance < 40% → rejected."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot(buy_volume=300.0, sell_volume=700.0)  # 30%
        rt.strategy_service = None

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is not None
        assert "CVD divergence" in reason

    def test_missing_data_skips_checks(self):
        """Missing funding/CVD → pre-filter passes (conservative)."""
        setup = _make_setup(direction="long")
        snapshot = MarketSnapshot(
            pair="ETH/USDT", timestamp=int(time.time() * 1000),
        )
        rt.strategy_service = None

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is None


# ============================================================
# Dedup cache
# ============================================================

class TestDedupCache:

    def test_dedup_blocks_duplicate_setup(self):
        """Same setup within TTL should not be executed twice."""
        import asyncio
        setup = _make_setup()
        decision = AIDecision(
            confidence=0.80, approved=True,
            reasoning="Good", adjustments={}, warnings=[],
        )
        approval = RiskApproval(
            approved=True, position_size=0.02, leverage=1.0,
            risk_pct=0.02, reason="",
        )
        _, _, _, _, execution, _ = _wire_services(
            setup=setup, decision=decision, approval=approval,
        )

        candle = _make_candle()
        asyncio.run(main.on_candle_confirmed(candle))
        asyncio.run(main.on_candle_confirmed(candle))

        # Execution should only happen once — second call is deduped
        assert execution.execute.call_count == 1


# ============================================================
# Branch coverage (Refactor Phase 6 — main.py split)
# Exercise the dual-thrust shadow hook, HTF-campaign intraday block, and
# position-guardian paths so "tests green" covers them before functions are
# relocated out of main.py in later phases.
# ============================================================

class TestUncoveredBranches:

    def test_dual_thrust_shadow_hook_invoked_on_eth_4h(self):
        import asyncio
        _wire_services(setup=None)
        dt = MagicMock()
        result = MagicMock()
        result.new_trades = []  # no flips → no persist/notify
        dt.on_candle.return_value = result
        rt.dual_thrust_shadow = dt

        with patch.object(main.settings, "DUAL_THRUST_SHADOW_ENABLED", True):
            asyncio.run(main.on_candle_confirmed(
                _make_candle(pair="ETH/USDT", timeframe="4h")))

        dt.on_candle.assert_called_once()

    def test_htf_campaign_active_blocks_intraday(self):
        import asyncio
        strategy, _data, _ai, _risk, _exec, _notif = _wire_services(
            setup=_make_setup())
        campaign = MagicMock()
        campaign.has_active_campaign.return_value = True
        rt.campaign_monitor = campaign

        # 5m candle != HTF_CAMPAIGN_SIGNAL_TF (4h): first HTF block is skipped,
        # then the active-campaign guard blocks the intraday path and returns.
        with patch.object(main.settings, "HTF_CAMPAIGN_ENABLED", True):
            asyncio.run(main.on_candle_confirmed(_make_candle(timeframe="5m")))

        strategy.evaluate_all.assert_not_called()

    def test_position_guardian_evaluated_when_enabled(self):
        import asyncio
        _strategy, data, _ai, _risk, execution, _notif = _wire_services(
            setup=None)
        guardian = AsyncMock()
        execution._guardian = guardian
        data.get_candles.return_value = [_make_candle()]

        with patch.object(main.settings, "POSITION_GUARDIAN_ENABLED", True):
            asyncio.run(main.on_candle_confirmed(_make_candle()))

        guardian.evaluate.assert_called_once()
