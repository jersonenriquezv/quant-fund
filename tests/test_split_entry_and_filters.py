"""
Tests for expectancy filters (ATR volatility + target space).
"""

import time
from unittest.mock import MagicMock

import pytest

from config.settings import settings
from shared.models import Candle, TradeSetup
from strategy_service.service import StrategyService
from strategy_service.market_structure import (
    MarketStructureState, StructureBreak, SwingPoint,
)
from strategy_service.order_blocks import OrderBlock
from tests.conftest import make_candle


# ================================================================
# Helpers
# ================================================================

def _make_ob(direction="bullish", body_high=102.0, body_low=100.0) -> OrderBlock:
    brk = StructureBreak(
        timestamp=10000, break_type="bos", direction=direction,
        break_price=110.0, broken_level=108.0, candle_index=10,
    )
    body_range = body_high - body_low
    entry = body_low + body_range * 0.5 if direction == "bullish" else body_high - body_range * 0.5
    return OrderBlock(
        timestamp=8000,
        pair="BTC/USDT",
        timeframe="15m",
        direction=direction,
        high=body_high + 1.0,
        low=body_low - 1.0,
        body_high=body_high,
        body_low=body_low,
        entry_price=entry,
        volume=20.0,
        volume_ratio=2.0,
        mitigated=False,
        associated_break=brk,
    )


def _make_candles(base_price=100.0, count=20, spread=1.0) -> list[Candle]:
    """Create candles with realistic ATR."""
    candles = []
    for i in range(count):
        candles.append(make_candle(
            open=base_price,
            high=base_price + spread,
            low=base_price - spread,
            close=base_price + 0.1,
            timestamp=i * 900_000,
        ))
    return candles


def _make_ms_state(
    trend="bullish",
    swing_highs=None,
    swing_lows=None,
) -> MarketStructureState:
    brk = StructureBreak(
        timestamp=10000, break_type="bos", direction="bullish",
        break_price=110.0, broken_level=108.0, candle_index=10,
    )
    return MarketStructureState(
        pair="BTC/USDT",
        timeframe="4h",
        trend=trend,
        swing_highs=swing_highs or [],
        swing_lows=swing_lows or [],
        structure_breaks=[brk],
        latest_break=brk,
    )


def _make_setup(
    direction="long", entry=100.0, sl=98.0, tp1=102.0, tp2=104.0,
    setup_type="setup_a",
) -> TradeSetup:
    return TradeSetup(
        timestamp=int(time.time()),
        pair="BTC/USDT",
        direction=direction,
        setup_type=setup_type,
        entry_price=entry,
        sl_price=sl,
        tp1_price=tp1,
        tp2_price=tp2,
        confluences=["ob", "sweep"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
    )


# ================================================================
# Expectancy filters — ATR volatility
# ================================================================

class TestATRFilter:
    """Test ATR volatility filter in StrategyService."""

    def _make_strategy_service(self):
        """Create a StrategyService with mocked DataService."""
        data = MagicMock()
        return StrategyService(data)

    def test_low_atr_rejects(self):
        """ATR / price < MIN_ATR_PCT → reject."""
        svc = self._make_strategy_service()

        # Candles with very tight range → low ATR
        candles = _make_candles(base_price=100.0, count=20, spread=0.05)
        setup = _make_setup(entry=100.0, sl=98.0)
        state_4h = _make_ms_state()
        state_1h = _make_ms_state()

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is not None
        assert "ATR too low" in result

    def test_normal_atr_passes(self):
        """ATR / price >= MIN_ATR_PCT → pass."""
        svc = self._make_strategy_service()

        # Candles with normal range → ATR ~2% of price
        candles = _make_candles(base_price=100.0, count=20, spread=2.0)
        setup = _make_setup(entry=100.0, sl=98.0)
        state_4h = _make_ms_state()
        state_1h = _make_ms_state()

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is None

    def test_insufficient_candles_passes(self):
        """With < 15 candles, ATR computation returns None → filter passes."""
        svc = self._make_strategy_service()

        candles = _make_candles(base_price=100.0, count=5, spread=0.01)
        setup = _make_setup(entry=100.0, sl=98.0)
        state_4h = _make_ms_state()
        state_1h = _make_ms_state()

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is None


# ================================================================
# 5. Expectancy filters — Target space
# ================================================================

class TestTargetSpaceFilter:
    """Test target space filter in StrategyService."""

    def _make_strategy_service(self):
        data = MagicMock()
        return StrategyService(data)

    def test_long_blocked_by_nearby_swing_high(self):
        """Long setup with swing high too close → reject."""
        svc = self._make_strategy_service()

        # Setup: entry=100, sl=98 → risk=2
        # MIN_TARGET_SPACE_R=1.2 → min space = 2*1.2 = 2.4
        # Swing high at 101.5 → space = 1.5 < 2.4 → reject
        candles = _make_candles(base_price=100.0, count=20, spread=2.0)
        setup = _make_setup(direction="long", entry=100.0, sl=98.0)
        state_4h = _make_ms_state(swing_highs=[
            SwingPoint(timestamp=5000, price=101.5, index=5, swing_type="high"),
        ])
        state_1h = _make_ms_state()

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is not None
        assert "Target space too tight" in result

    def test_long_with_distant_swing_high_passes(self):
        """Long setup with swing high far away → pass."""
        svc = self._make_strategy_service()

        # Setup: entry=100, sl=98 → risk=2
        # Swing high at 110 → space = 10 > 2.4 → pass
        candles = _make_candles(base_price=100.0, count=20, spread=2.0)
        setup = _make_setup(direction="long", entry=100.0, sl=98.0)
        state_4h = _make_ms_state(swing_highs=[
            SwingPoint(timestamp=5000, price=110.0, index=5, swing_type="high"),
        ])
        state_1h = _make_ms_state()

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is None

    def test_short_blocked_by_nearby_swing_low(self):
        """Short setup with swing low too close → reject."""
        svc = self._make_strategy_service()

        # Setup: entry=100, sl=102 → risk=2
        # Swing low at 99.0 → space = 1.0 < 2.4 → reject
        candles = _make_candles(base_price=100.0, count=20, spread=2.0)
        setup = _make_setup(direction="short", entry=100.0, sl=102.0, tp1=98.0, tp2=96.0)
        state_4h = _make_ms_state(swing_lows=[
            SwingPoint(timestamp=5000, price=99.0, index=5, swing_type="low"),
        ])
        state_1h = _make_ms_state()

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is not None
        assert "Target space too tight" in result

    def test_no_opposing_swings_passes(self):
        """No opposing swings in the way → filter passes."""
        svc = self._make_strategy_service()

        candles = _make_candles(base_price=100.0, count=20, spread=2.0)
        setup = _make_setup(direction="long", entry=100.0, sl=98.0)
        state_4h = _make_ms_state(swing_highs=[], swing_lows=[])
        state_1h = _make_ms_state(swing_highs=[], swing_lows=[])

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is None

    def test_swing_below_entry_ignored_for_long(self):
        """Swing highs BELOW entry should not block a long setup."""
        svc = self._make_strategy_service()

        candles = _make_candles(base_price=100.0, count=20, spread=2.0)
        setup = _make_setup(direction="long", entry=100.0, sl=98.0)
        state_4h = _make_ms_state(swing_highs=[
            SwingPoint(timestamp=5000, price=95.0, index=5, swing_type="high"),
        ])
        state_1h = _make_ms_state()

        result = svc._apply_expectancy_filters(setup, candles, state_4h, state_1h)
        assert result is None


# ================================================================
# 6. ATR computation standalone
# ================================================================

class TestComputeATR:
    """Test _compute_atr static method."""

    def test_atr_computation(self):
        """ATR should be average of True Range over period."""
        # Create candles with known TR:
        # TR = max(high-low, |high-prev_close|, |low-prev_close|)
        candles = []
        for i in range(20):
            candles.append(make_candle(
                open=100.0, high=102.0, low=98.0, close=100.0,
                timestamp=i * 900_000,
            ))
        atr = StrategyService._compute_atr(candles, 14)
        # Each candle: high-low=4, |high-prev_close|=2, |low-prev_close|=2
        # TR = max(4,2,2) = 4
        # ATR = avg of 14 TRs of 4.0 = 4.0
        assert atr == pytest.approx(4.0)

    def test_atr_returns_none_insufficient_data(self):
        candles = [make_candle(timestamp=i * 900_000) for i in range(10)]
        atr = StrategyService._compute_atr(candles, 14)
        assert atr is None
