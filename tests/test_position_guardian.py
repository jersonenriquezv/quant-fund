"""
Tests for Position Guardian — active position monitoring.

Tests cover:
- Counter-structure detection (consecutive candles against position)
- Momentum death (body size decay) in profit vs loss
- Stall detection (low range) in loss vs profit
- Adverse CVD divergence
- No position / disabled scenarios
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution_service.position_guardian import PositionGuardian
from execution_service.models import ManagedPosition
from shared.models import Candle, CVDSnapshot


# ================================================================
# Helpers
# ================================================================

def _make_candle(
    open: float, close: float, high: float = 0, low: float = 0,
    pair: str = "BTC/USDT", timeframe: str = "5m",
    timestamp: int = 0,
) -> Candle:
    """Create a Candle with sensible defaults for high/low."""
    if high == 0:
        high = max(open, close) * 1.001
    if low == 0:
        low = min(open, close) * 0.999
    if timestamp == 0:
        timestamp = int(time.time() * 1000)
    return Candle(
        timestamp=timestamp,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=10.0,
        volume_quote=1000.0,
        pair=pair,
        timeframe=timeframe,
        confirmed=True,
    )


def _make_position(
    pair: str = "BTC/USDT",
    direction: str = "long",
    entry_price: float = 80000.0,
    sl_price: float = 79000.0,
    phase: str = "active",
    current_sl_price: float = 0.0,
) -> ManagedPosition:
    """Create a ManagedPosition for testing."""
    pos = ManagedPosition(
        pair=pair,
        direction=direction,
        setup_type="setup_a",
        phase=phase,
        entry_price=entry_price,
        actual_entry_price=entry_price,
        sl_price=sl_price,
        current_sl_price=current_sl_price or sl_price,
        tp1_price=81000.0,
        tp2_price=83000.0,
        filled_size=0.01,
        leverage=7.0,
        created_at=int(time.time()),
        filled_at=int(time.time()),
    )
    return pos


def _make_monitor_mock(pos=None):
    """Create a mock PositionMonitor with optional position."""
    monitor = MagicMock()
    if pos:
        monitor.positions = {pos.pair: pos}
    else:
        monitor.positions = {}
    monitor._close_all_orders_and_market_close = AsyncMock()
    monitor._adjust_sl = AsyncMock()
    return monitor


# ================================================================
# Test: Counter-structure detection
# ================================================================

class TestCounterStructure:
    def test_long_3_red_candles_closes(self):
        """3 consecutive red candles while long -> close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(79000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 80500),
            _make_candle(80500, 80200),  # red
            _make_candle(80200, 79900),  # red
            _make_candle(79900, 79600),  # red
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        assert result == "close"
        monitor._close_all_orders_and_market_close.assert_awaited_once()

    def test_short_3_green_candles_closes(self):
        """3 consecutive green candles while short -> close."""
        pos = _make_position(direction="short", entry_price=80000.0, sl_price=81000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(80500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 79000),
            _make_candle(79000, 79300),  # green
            _make_candle(79300, 79600),  # green
            _make_candle(79600, 79900),  # green
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        assert result == "close"
        monitor._close_all_orders_and_market_close.assert_awaited_once()

    def test_mixed_candles_no_action(self):
        """Mixed candles -> no action."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(80000, 80500),
            _make_candle(80500, 80200),  # red
            _make_candle(80200, 80600),  # green
            _make_candle(80600, 80300),  # red
            _make_candle(80300, 80100),  # red
            _make_candle(80100, 80400),  # green <- breaks the streak
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        assert result is None
        monitor._close_all_orders_and_market_close.assert_not_awaited()


# ================================================================
# Test: Momentum death
# ================================================================

class TestMomentumDeath:
    def test_in_profit_tightens_sl(self):
        """Momentum decay while in profit -> tighten SL to breakeven."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        # Reference: big bodies ($500). Recent: tiny bodies ($30).
        candles = [
            _make_candle(79000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 80500),
            _make_candle(80500, 80530),  # in profit, tiny body
            _make_candle(80530, 80550),
            _make_candle(80550, 80560),
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        assert result == "tighten_sl"
        monitor._adjust_sl.assert_awaited_once_with(pos, 80000.0)

    def test_in_loss_closes(self):
        """Momentum decay while in loss -> close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        # Reference: big bodies. Recent: tiny bodies, price below entry.
        candles = [
            _make_candle(80500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 79000),
            _make_candle(79800, 79810),  # tiny, in loss
            _make_candle(79810, 79820),
            _make_candle(79820, 79830),
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        assert result == "close"
        monitor._close_all_orders_and_market_close.assert_awaited_once()


# ================================================================
# Test: Stall detection
# ================================================================

class TestStallDetection:
    def test_stall_in_loss_closes(self):
        """Price stalling while in loss -> close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        base = 79900.0  # Below entry = in loss
        candles = [
            _make_candle(79800, 80200, high=80300, low=79700),
            _make_candle(80200, 79800, high=80300, low=79700),
            _make_candle(79800, 80200, high=80300, low=79700),
            _make_candle(base, base + 1, high=base + 2, low=base - 2),
            _make_candle(base + 1, base - 1, high=base + 2, low=base - 2),
            _make_candle(base - 1, base, high=base + 2, low=base - 2),
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        # Either momentum_death or stall will close the losing position
        assert result == "close"

    def test_stall_in_profit_no_close(self):
        """Price stalling while in profit -> no market close."""
        pos = _make_position(direction="long", entry_price=79000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        base = 80000.0  # Above entry = in profit
        candles = [
            _make_candle(base, base + 200, high=base + 300, low=base - 100),
            _make_candle(base + 200, base, high=base + 300, low=base - 100),
            _make_candle(base, base + 200, high=base + 300, low=base - 100),
            _make_candle(base, base + 10, high=base + 15, low=base - 5),
            _make_candle(base + 10, base - 5, high=base + 15, low=base - 10),
            _make_candle(base - 5, base + 5, high=base + 15, low=base - 10),
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        # Stall check skips profitable positions.
        # Momentum death may fire with tighten_sl, but never close.
        monitor._close_all_orders_and_market_close.assert_not_awaited()


# ================================================================
# Test: Adverse CVD divergence
# ================================================================

class TestAdverseCVD:
    def test_long_in_loss_negative_cvd_closes(self):
        """Long in loss + negative CVD -> close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        # Mixed candles to avoid counter-structure
        candles = [
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),  # close < entry = loss
        ]
        current = candles[-1]

        cvd = CVDSnapshot(
            timestamp=int(time.time() * 1000),
            pair="BTC/USDT",
            cvd_5m=-100.0,
            cvd_15m=-200.0,
            cvd_1h=-500.0,
            buy_volume=1000.0,
            sell_volume=2000.0,
        )

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles, cvd))

        assert result == "close"
        monitor._close_all_orders_and_market_close.assert_awaited_once()

    def test_long_in_profit_negative_cvd_no_action(self):
        """Long in profit + negative CVD -> no close."""
        pos = _make_position(direction="long", entry_price=79000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 80500),  # in profit
        ]
        current = candles[-1]

        cvd = CVDSnapshot(
            timestamp=int(time.time() * 1000),
            pair="BTC/USDT",
            cvd_5m=-100.0,
            cvd_15m=-200.0,
            cvd_1h=-500.0,
            buy_volume=1000.0,
            sell_volume=2000.0,
        )

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles, cvd))

        assert result is None
        monitor._close_all_orders_and_market_close.assert_not_awaited()

    def test_short_in_loss_positive_cvd_closes(self):
        """Short in loss + positive CVD -> close."""
        pos = _make_position(
            direction="short", entry_price=80000.0, sl_price=81000.0
        )
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(80500, 80000),
            _make_candle(80000, 80500),
            _make_candle(80500, 80000),
            _make_candle(80000, 80500),
            _make_candle(80500, 80000),
            _make_candle(80000, 80500),  # above entry = loss for short
        ]
        current = candles[-1]

        cvd = CVDSnapshot(
            timestamp=int(time.time() * 1000),
            pair="BTC/USDT",
            cvd_5m=100.0,
            cvd_15m=200.0,
            cvd_1h=500.0,
            buy_volume=2000.0,
            sell_volume=1000.0,
        )

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles, cvd))

        assert result == "close"


# ================================================================
# Test: No position / disabled / edge cases
# ================================================================

class TestEdgeCases:
    def test_no_position_no_action(self):
        """No active position for the pair -> no action."""
        monitor = _make_monitor_mock()
        guardian = PositionGuardian(monitor)

        candles = [_make_candle(80000, 79500) for _ in range(6)]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))
        assert result is None

    def test_pending_position_no_action(self):
        """Position in pending_entry phase -> no action."""
        pos = _make_position(phase="pending_entry")
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [_make_candle(80000, 79500) for _ in range(6)]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))
        assert result is None

    def test_disabled_no_action(self):
        """Guardian disabled via settings -> no action."""
        pos = _make_position()
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [_make_candle(80000, 79500) for _ in range(6)]
        current = candles[-1]

        with patch("execution_service.position_guardian.settings") as mock_settings:
            mock_settings.POSITION_GUARDIAN_ENABLED = False
            result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))

        assert result is None
        monitor._close_all_orders_and_market_close.assert_not_awaited()

    def test_cvd_none_no_crash(self):
        """CVD is None -> CVD check skipped, no crash."""
        pos = _make_position(direction="long", entry_price=79000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        # Non-triggering mixed candles, in profit
        candles = [
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 80500),
        ]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles, cvd=None))
        assert result is None

    def test_too_few_candles_no_action(self):
        """Fewer than GUARDIAN_COUNTER_CANDLES -> no action."""
        pos = _make_position()
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [_make_candle(80000, 79500), _make_candle(80000, 79500)]
        current = candles[-1]

        result = asyncio.run(guardian.evaluate("BTC/USDT", current, candles))
        assert result is None
