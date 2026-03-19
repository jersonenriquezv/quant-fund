"""
Tests for Position Guardian — shadow-mode position monitoring.

Guardian is in shadow mode: all checks evaluate and log but never
close positions or adjust SL. Shadow triggers are persisted to ml_setups
for feature importance analysis.

Tests verify:
- Shadow checks detect conditions correctly (log output)
- Shadow triggers are persisted to DB via _record_shadow
- Guardian NEVER calls close or adjust SL (shadow mode contract)
- No position / disabled scenarios still short-circuit
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

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
    if high == 0:
        high = max(open, close) * 1.001
    if low == 0:
        low = min(open, close) * 0.999
    if timestamp == 0:
        timestamp = int(time.time() * 1000)
    return Candle(
        timestamp=timestamp, open=open, high=high, low=low, close=close,
        volume=10.0, volume_quote=1000.0, pair=pair, timeframe=timeframe,
        confirmed=True,
    )


def _make_position(
    pair: str = "BTC/USDT", direction: str = "long",
    entry_price: float = 80000.0, sl_price: float = 79000.0,
    phase: str = "active", setup_id: str = "test_setup_123",
) -> ManagedPosition:
    return ManagedPosition(
        pair=pair, direction=direction, setup_type="setup_a", phase=phase,
        entry_price=entry_price, actual_entry_price=entry_price,
        sl_price=sl_price, current_sl_price=sl_price,
        tp1_price=81000.0, tp2_price=83000.0, filled_size=0.01,
        leverage=7.0, created_at=int(time.time()), filled_at=int(time.time()),
        setup_id=setup_id,
    )


def _make_monitor_mock(pos=None):
    monitor = MagicMock()
    if pos:
        monitor.positions = {pos.pair: pos}
    else:
        monitor.positions = {}
    # Mock data_store for shadow persistence
    monitor._data_store = MagicMock()
    monitor._data_store.postgres.update_ml_guardian_shadow = MagicMock(return_value=True)
    return monitor


# ================================================================
# Test: Shadow mode contract — NEVER acts, always returns None
# ================================================================

class TestShadowModeContract:
    def test_counter_structure_shadow_only(self):
        """5 red candles while long -> shadow log + DB write, no close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(79000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 80500),
            _make_candle(80500, 80300),
            _make_candle(80300, 80200),
            _make_candle(80200, 80100),
            _make_candle(80100, 79900),
            _make_candle(79900, 79800),
            _make_candle(79800, 79700),
            _make_candle(79700, 79600),
            _make_candle(79600, 79500),  # 5 consecutive red
        ]

        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))

        assert result is None
        monitor._data_store.postgres.update_ml_guardian_shadow.assert_any_call(
            "test_setup_123", "counter"
        )

    def test_momentum_death_shadow_only(self):
        """Momentum decay in loss -> shadow log + DB, no close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(80500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 79000),
            _make_candle(79000, 78500),
            _make_candle(78500, 78000),
            _make_candle(79800, 79810),  # tiny, in loss
            _make_candle(79810, 79820),
            _make_candle(79820, 79830),
            _make_candle(79830, 79840),
            _make_candle(79840, 79850),
        ]

        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))

        assert result is None
        monitor._data_store.postgres.update_ml_guardian_shadow.assert_any_call(
            "test_setup_123", "momentum"
        )

    def test_stall_in_loss_shadow_only(self):
        """Stall while in loss -> shadow log + DB, no close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        base = 79900.0
        candles = [
            _make_candle(79800, 80200, high=80300, low=79700),
            _make_candle(80200, 79800, high=80300, low=79700),
            _make_candle(79800, 80200, high=80300, low=79700),
            _make_candle(80200, 79800, high=80300, low=79700),
            _make_candle(79800, 80200, high=80300, low=79700),
            _make_candle(base, base + 1, high=base + 2, low=base - 2),
            _make_candle(base + 1, base - 1, high=base + 2, low=base - 2),
            _make_candle(base - 1, base, high=base + 2, low=base - 2),
            _make_candle(base, base + 1, high=base + 2, low=base - 2),
            _make_candle(base + 1, base, high=base + 2, low=base - 2),
        ]

        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))

        assert result is None

    def test_adverse_cvd_shadow_only(self):
        """Adverse CVD in loss -> shadow log + DB, no close."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
            _make_candle(79500, 80000),
            _make_candle(80000, 79500),
        ]

        cvd = CVDSnapshot(
            timestamp=int(time.time() * 1000), pair="BTC/USDT",
            cvd_5m=-100.0, cvd_15m=-200.0, cvd_1h=-500.0,
            buy_volume=1000.0, sell_volume=2000.0,
        )

        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles, cvd))

        assert result is None
        monitor._data_store.postgres.update_ml_guardian_shadow.assert_any_call(
            "test_setup_123", "cvd"
        )


# ================================================================
# Test: Dedup — only writes to DB once per trade per check
# ================================================================

class TestShadowDedup:
    def test_counter_fires_once_per_trade(self):
        """Counter-structure shadow should write to DB only once."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(80500, 80300),
            _make_candle(80300, 80200),
            _make_candle(80200, 80100),
            _make_candle(80100, 79900),
            _make_candle(79900, 79800),
            _make_candle(79800, 79700),
            _make_candle(79700, 79600),
            _make_candle(79600, 79500),
            _make_candle(79500, 79400),
            _make_candle(79400, 79300),
        ]

        # Fire twice on same position
        asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))
        asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))

        # DB write only once for "counter"
        counter_calls = [
            c for c in monitor._data_store.postgres.update_ml_guardian_shadow.call_args_list
            if c[0] == ("test_setup_123", "counter")
        ]
        assert len(counter_calls) == 1

    def test_cleanup_resets_tracking(self):
        """After cleanup, the same check can fire again (new trade)."""
        pos = _make_position(direction="long", entry_price=80000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        guardian._fired["test_setup_123"] = {"counter"}
        guardian.cleanup("test_setup_123")

        assert "test_setup_123" not in guardian._fired


# ================================================================
# Test: Edge cases
# ================================================================

class TestEdgeCases:
    def test_no_position_no_action(self):
        monitor = _make_monitor_mock()
        guardian = PositionGuardian(monitor)
        candles = [_make_candle(80000, 79500) for _ in range(6)]
        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))
        assert result is None

    def test_pending_position_no_action(self):
        pos = _make_position(phase="pending_entry")
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)
        candles = [_make_candle(80000, 79500) for _ in range(6)]
        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))
        assert result is None

    def test_disabled_no_action(self):
        pos = _make_position()
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)
        candles = [_make_candle(80000, 79500) for _ in range(6)]
        with patch("execution_service.position_guardian.settings") as mock_settings:
            mock_settings.POSITION_GUARDIAN_ENABLED = False
            result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))
        assert result is None

    def test_cvd_none_no_crash(self):
        pos = _make_position(direction="long", entry_price=79000.0)
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)
        candles = [
            _make_candle(79500, 80000), _make_candle(80000, 79500),
            _make_candle(79500, 80000), _make_candle(80000, 79500),
            _make_candle(79500, 80000), _make_candle(80000, 79500),
            _make_candle(79500, 80000), _make_candle(80000, 79500),
            _make_candle(79500, 80000), _make_candle(80000, 80500),
        ]
        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles, cvd=None))
        assert result is None

    def test_too_few_candles_no_action(self):
        pos = _make_position()
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)
        candles = [_make_candle(80000, 79500), _make_candle(80000, 79500)]
        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))
        assert result is None

    def test_empty_setup_id_no_db_write(self):
        """Position with no setup_id -> shadow log but no DB write."""
        pos = _make_position(setup_id="")
        monitor = _make_monitor_mock(pos)
        guardian = PositionGuardian(monitor)

        candles = [
            _make_candle(80500, 80300),
            _make_candle(80300, 80200),
            _make_candle(80200, 80100),
            _make_candle(80100, 79900),
            _make_candle(79900, 79800),
            _make_candle(79800, 79700),
            _make_candle(79700, 79600),
            _make_candle(79600, 79500),
            _make_candle(79500, 79400),
            _make_candle(79400, 79300),
        ]

        result = asyncio.run(guardian.evaluate("BTC/USDT", candles[-1], candles))

        assert result is None
        monitor._data_store.postgres.update_ml_guardian_shadow.assert_not_called()
