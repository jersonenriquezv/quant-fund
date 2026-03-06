"""Tests for main.py — pipeline wiring and pre-filter logic.

Mocks all 5 services to test the on_candle_confirmed callback
and _pre_filter_for_claude / _evaluate_with_claude logic.
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from shared.models import (
    Candle, TradeSetup, AIDecision, RiskApproval,
    MarketSnapshot, FundingRate, CVDSnapshot,
)
import main


# ============================================================
# Fixtures
# ============================================================

def _make_candle(pair="BTC/USDT", close=50000.0, timeframe="5m") -> Candle:
    return Candle(
        timestamp=int(time.time() * 1000),
        open=close - 50, high=close + 50, low=close - 100,
        close=close, volume=1.0, volume_quote=close,
        pair=pair, timeframe=timeframe, confirmed=True,
    )


def _make_setup(pair="BTC/USDT", direction="long") -> TradeSetup:
    entry = 50000.0
    sl = 49000.0 if direction == "long" else 51000.0
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair, direction=direction, setup_type="setup_a",
        entry_price=entry, sl_price=sl,
        tp1_price=51000.0 if direction == "long" else 49000.0,
        tp2_price=52000.0 if direction == "long" else 48000.0,
        tp3_price=53000.0 if direction == "long" else 47000.0,
        confluences=["choch", "ob", "sweep"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
    )


def _make_snapshot(
    pair="BTC/USDT",
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


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset main.py module-level state between tests."""
    main._setup_dedup_cache.clear()
    main._data_service = None
    main._strategy_service = None
    main._ai_service = None
    main._risk_service = None
    main._execution_service = None
    main._notifier = None
    yield
    main._setup_dedup_cache.clear()
    main._data_service = None
    main._strategy_service = None
    main._ai_service = None
    main._risk_service = None
    main._execution_service = None
    main._notifier = None


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
    strategy.get_htf_bias.return_value = htf_bias
    strategy.get_active_order_blocks.return_value = []
    main._strategy_service = strategy

    data = MagicMock()
    data.get_market_snapshot.return_value = snapshot or _make_snapshot()
    data.redis = MagicMock()
    data.redis.get_bot_state.return_value = None
    data.postgres = MagicMock()
    data.postgres.insert_ai_decision.return_value = 1
    main._data_service = data

    ai = AsyncMock()
    if decision is not None:
        ai.evaluate.return_value = decision
    main._ai_service = ai

    risk = MagicMock()
    if approval is not None:
        risk.check.return_value = approval
    main._risk_service = risk

    execution = AsyncMock()
    execution.execute.return_value = True
    main._execution_service = execution

    notifier = AsyncMock()
    main._notifier = notifier

    return strategy, data, ai, risk, execution, notifier


# ============================================================
# Full pipeline — happy path
# ============================================================

class TestPipelineHappyPath:

    @pytest.mark.asyncio
    async def test_setup_approved_and_executed(self):
        """Full pipeline: setup → AI approves → risk approves → executed."""
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

        await main.on_candle_confirmed(_make_candle())

        ai.evaluate.assert_called_once()
        risk.check.assert_called_once_with(setup)
        execution.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_setup_detected_stops_early(self):
        """No setup → pipeline stops after strategy."""
        _, _, ai, risk, execution, _ = _wire_services(setup=None)

        await main.on_candle_confirmed(_make_candle())

        ai.evaluate.assert_not_called()
        risk.check.assert_not_called()
        execution.execute.assert_not_called()


# ============================================================
# AI rejection
# ============================================================

class TestAIRejection:

    @pytest.mark.asyncio
    async def test_ai_rejects_stops_pipeline(self):
        """AI rejects setup → risk and execution never called."""
        setup = _make_setup()
        decision = AIDecision(
            confidence=0.40, approved=False,
            reasoning="CVD divergence", adjustments={}, warnings=[],
        )
        _, _, _, risk, execution, _ = _wire_services(
            setup=setup, decision=decision,
        )

        await main.on_candle_confirmed(_make_candle())

        risk.check.assert_not_called()
        execution.execute.assert_not_called()


# ============================================================
# Risk rejection
# ============================================================

class TestRiskRejection:

    @pytest.mark.asyncio
    async def test_risk_rejects_stops_execution(self):
        """Risk rejects → execution never called."""
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

        await main.on_candle_confirmed(_make_candle())

        execution.execute.assert_not_called()


# ============================================================
# Pre-filter
# ============================================================

class TestPreFilter:

    def test_htf_conflict_long_bearish(self):
        """Long + HTF bearish → rejected."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot()
        strategy = MagicMock()
        strategy.get_htf_bias.return_value = "bearish"
        main._strategy_service = strategy

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is not None
        assert "long" in reason and "bearish" in reason

    def test_htf_conflict_short_bullish(self):
        """Short + HTF bullish → rejected."""
        setup = _make_setup(direction="short")
        snapshot = _make_snapshot()
        strategy = MagicMock()
        strategy.get_htf_bias.return_value = "bullish"
        main._strategy_service = strategy

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is not None
        assert "short" in reason and "bullish" in reason

    def test_htf_aligned_passes(self):
        """Long + HTF bullish → passes pre-filter."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot()
        strategy = MagicMock()
        strategy.get_htf_bias.return_value = "bullish"
        main._strategy_service = strategy

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is None

    def test_funding_extreme_long(self):
        """Long + extreme positive funding → rejected."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot(funding_rate=0.001)  # 0.1% >> 0.03%
        main._strategy_service = None  # skip HTF check

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is not None
        assert "Funding extreme" in reason

    def test_cvd_divergence_long(self):
        """Long + buy dominance < 40% → rejected."""
        setup = _make_setup(direction="long")
        snapshot = _make_snapshot(buy_volume=300.0, sell_volume=700.0)  # 30%
        main._strategy_service = None

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is not None
        assert "CVD divergence" in reason

    def test_missing_data_skips_checks(self):
        """Missing funding/CVD → pre-filter passes (conservative)."""
        setup = _make_setup(direction="long")
        snapshot = MarketSnapshot(
            pair="BTC/USDT", timestamp=int(time.time() * 1000),
        )
        main._strategy_service = None

        reason = main._pre_filter_for_claude(setup, snapshot)
        assert reason is None


# ============================================================
# Dedup cache
# ============================================================

class TestDedupCache:

    @pytest.mark.asyncio
    async def test_dedup_blocks_duplicate_setup(self):
        """Same setup within TTL should not be sent to Claude twice."""
        setup = _make_setup()
        decision = AIDecision(
            confidence=0.80, approved=True,
            reasoning="Good", adjustments={}, warnings=[],
        )
        approval = RiskApproval(
            approved=True, position_size=0.02, leverage=1.0,
            risk_pct=0.02, reason="",
        )
        _, _, ai, _, _, _ = _wire_services(
            setup=setup, decision=decision, approval=approval,
        )

        candle = _make_candle()
        await main.on_candle_confirmed(candle)
        await main.on_candle_confirmed(candle)

        # Claude should only be called once — second call is deduped
        assert ai.evaluate.call_count == 1
