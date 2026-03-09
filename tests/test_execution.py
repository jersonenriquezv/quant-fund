"""
Tests for Execution Service (Layer 5).

All ccxt calls are mocked. Tests verify business logic only:
- Happy path: entry placed, position registered
- Disabled without API key
- Entry fill → SL + single TP placement
- Entry timeout → cancel
- TP fill → position closed
- SL fill → position closed
- Breakeven trigger → SL moves to entry
- 12h timeout → market close
- Emergency close on SL placement failure
- Slippage logging
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import settings
from shared.models import TradeSetup, RiskApproval
from execution_service.models import ManagedPosition
from execution_service.executor import OrderExecutor
from execution_service.monitor import PositionMonitor
from execution_service.service import ExecutionService


# ================================================================
# Fixtures
# ================================================================

def make_setup(
    pair="ETH/USDT",
    direction="long",
    entry=2000.0,
    sl=1960.0,
    tp1=2040.0,
    tp2=2080.0,
    tp3=2120.0,
) -> TradeSetup:
    return TradeSetup(
        timestamp=int(time.time()),
        pair=pair,
        direction=direction,
        setup_type="setup_a",
        entry_price=entry,
        sl_price=sl,
        tp1_price=tp1,
        tp2_price=tp2,
        tp3_price=tp3,
        confluences=["ob", "fvg", "sweep"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
    )


def make_approval(size=0.05, leverage=3.0) -> RiskApproval:
    return RiskApproval(
        approved=True,
        position_size=size,
        leverage=leverage,
        risk_pct=0.02,
        reason="All checks passed",
    )


def make_order(order_id="ord-123", status="open", filled=0, average=0, price=2000.0):
    return {
        "id": order_id,
        "status": status,
        "filled": filled,
        "average": average,
        "price": price,
    }


def _mock_executor():
    """Create a mock executor with contracts_to_base as identity (tests use abstract units)."""
    executor = MagicMock(spec=OrderExecutor)
    executor.contracts_to_base = MagicMock(side_effect=lambda pair, v: v)
    return executor


def _make_service(executor=None, monitor=None, risk=None):
    """Create an ExecutionService with injected mocks (bypass __init__)."""
    service = ExecutionService.__new__(ExecutionService)
    service._enabled = True
    service._executor = executor or _mock_executor()
    service._monitor = monitor or MagicMock(spec=PositionMonitor)
    service._risk = risk or MagicMock()
    return service


def make_position(
    pair="ETH/USDT",
    direction="long",
    phase="pending_entry",
    entry_price=2000.0,
    sl_price=1960.0,
    size=0.05,
) -> ManagedPosition:
    return ManagedPosition(
        pair=pair,
        direction=direction,
        setup_type="setup_a",
        phase=phase,
        entry_price=entry_price,
        sl_price=sl_price,
        tp1_price=2040.0,    # Breakeven trigger (1:1 R:R)
        tp2_price=2080.0,    # TP order level (2:1 R:R)
        tp3_price=2120.0,
        total_size=size,
        filled_size=size if phase != "pending_entry" else 0.0,
        leverage=3.0,
        entry_order_id="ord-entry",
        created_at=int(time.time()),
    )


# ================================================================
# ExecutionService — Facade tests
# ================================================================

class TestExecutionServiceDisabled:
    """Service disabled when OKX_API_KEY is not set."""

    @patch("execution_service.service.settings")
    def test_disabled_without_api_key(self, mock_settings):
        mock_settings.OKX_API_KEY = ""
        risk = MagicMock()
        service = ExecutionService(risk)
        assert not service._enabled

    @patch("execution_service.service.settings")
    def test_execute_returns_false_when_disabled(self, mock_settings):
        mock_settings.OKX_API_KEY = ""
        risk = MagicMock()
        service = ExecutionService(risk)
        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))
        assert result is False


class TestExecutionServiceExecute:
    """Happy path: entry placed, position registered."""

    @pytest.fixture(autouse=True)
    def _sandbox_mode(self, monkeypatch):
        monkeypatch.setattr(settings, "OKX_SANDBOX", True)

    def test_happy_path_entry_placed(self):
        risk = MagicMock()
        executor = _mock_executor()
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}

        executor.configure_pair = AsyncMock(return_value=True)
        executor.fetch_ticker = AsyncMock(return_value={"ask": 2000.0, "bid": 1999.0})
        executor.place_limit_order = AsyncMock(return_value=make_order("ord-1"))

        service = _make_service(executor, monitor, risk)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))

        assert result is True
        executor.configure_pair.assert_called_once_with("ETH/USDT", 3)
        executor.place_limit_order.assert_called_once()
        call_args = executor.place_limit_order.call_args
        assert call_args[0][0] == "ETH/USDT"   # pair
        assert call_args[0][1] == "buy"          # side
        assert call_args[0][2] == 0.05           # size
        monitor.register.assert_called_once()
        risk.on_trade_opened.assert_called_once()

    def test_short_uses_sell_side(self):
        risk = MagicMock()
        executor = _mock_executor()
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}

        executor.configure_pair = AsyncMock(return_value=True)
        executor.fetch_ticker = AsyncMock(return_value={"ask": 2001.0, "bid": 2000.0})
        executor.place_limit_order = AsyncMock(return_value=make_order("ord-2"))

        service = _make_service(executor, monitor, risk)

        setup = make_setup(
            direction="short", entry=2000.0, sl=2040.0,
            tp1=1960.0, tp2=1920.0, tp3=1880.0
        )
        result = asyncio.run(
            service.execute(setup, make_approval(), 0.70)
        )

        assert result is True
        call_args = executor.place_limit_order.call_args
        assert call_args[0][1] == "sell"  # side

    def test_skips_if_pair_has_active_position(self):
        """Active (filled) positions block new entries for the same pair."""
        monitor = MagicMock(spec=PositionMonitor)
        active_pos = MagicMock()
        active_pos.phase = "active"
        active_pos.setup_type = "setup_a"
        monitor.positions = {"ETH/USDT": active_pos}

        service = _make_service(monitor=monitor)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))
        assert result is False

    def test_replaces_pending_entry_with_new_setup(self):
        """Pending (unfilled) entries are cancelled and replaced by new setups."""
        risk = MagicMock()
        executor = _mock_executor()
        monitor = MagicMock(spec=PositionMonitor)

        # Existing pending position
        old_pos = MagicMock()
        old_pos.phase = "pending_entry"
        old_pos.direction = "short"
        old_pos.entry_price = 2108.26
        monitor.positions = {"ETH/USDT": old_pos}

        def side_effect_cancel(pair):
            monitor.positions = {}
            return old_pos
        monitor.cancel_and_remove_pending = AsyncMock(side_effect=side_effect_cancel)

        executor.configure_pair = AsyncMock(return_value=True)
        executor.fetch_ticker = AsyncMock(return_value={"ask": 2050.0, "bid": 2049.0})
        executor.place_limit_order = AsyncMock(return_value=make_order("ord-new"))

        service = _make_service(executor, monitor, risk)

        setup = make_setup(
            pair="ETH/USDT", direction="short", entry=2077.0, sl=2084.0,
            tp1=2070.0, tp2=2063.0, tp3=2056.0
        )
        result = asyncio.run(service.execute(setup, make_approval(size=0.05), 0.75))

        assert result is True
        monitor.cancel_and_remove_pending.assert_called_once_with("ETH/USDT")
        risk.on_trade_cancelled.assert_called_once_with("ETH/USDT", "short")
        executor.place_limit_order.assert_called_once()

    def test_returns_false_on_configure_failure(self):
        executor = _mock_executor()
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        executor.configure_pair = AsyncMock(return_value=False)

        service = _make_service(executor, monitor)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))
        assert result is False

    def test_returns_false_on_order_failure(self):
        executor = _mock_executor()
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        executor.configure_pair = AsyncMock(return_value=True)
        executor.fetch_ticker = AsyncMock(return_value={"ask": 2000.0, "bid": 1999.0})
        executor.place_limit_order = AsyncMock(return_value=None)

        service = _make_service(executor, monitor)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))
        assert result is False


# ================================================================
# PositionMonitor — State machine tests
# ================================================================

class TestEntryFill:
    """Entry order fills → SL + single TP placed."""

    def test_entry_fill_places_sl_and_tp(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="closed", filled=0.05, average=2001.0
        ))
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl"))
        executor.place_take_profit = AsyncMock(return_value=make_order("ord-tp"))

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "active"
        assert pos.actual_entry_price == 2001.0
        assert pos.filled_size == 0.05
        assert pos.sl_order_id == "ord-sl"
        assert pos.tp_order_id == "ord-tp"

        # SL placed for full size
        sl_call = executor.place_stop_market.call_args
        assert sl_call[0][2] == 0.05  # full size
        assert sl_call[0][3] == 1960.0  # sl_price

        # TP placed at tp2_price for full size
        tp_call = executor.place_take_profit.call_args
        assert tp_call[0][2] == 0.05  # full size
        assert tp_call[0][3] == 2080.0  # tp2_price (2:1 R:R)


class TestEntryTimeout:
    """Entry not filled within timeout → cancel."""

    def test_entry_timeout_cancels_order(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.created_at = int(time.time()) - 15000  # Well past 4h
        monitor.register(pos)

        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        executor.cancel_order.assert_called_once_with("ord-entry", "ETH/USDT")
        assert pos.phase == "closed"
        assert pos.close_reason == "cancelled"
        risk.on_trade_closed.assert_not_called()
        risk.on_trade_cancelled.assert_called_once_with("ETH/USDT", "long")

    def test_quick_setup_uses_shorter_timeout(self):
        """Quick setups (C/D/E) use ENTRY_TIMEOUT_QUICK_SECONDS."""
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.setup_type = "setup_c"
        pos.created_at = int(time.time()) - 7200  # 2h — past 1h quick timeout
        monitor.register(pos)

        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "cancelled"

    def test_swing_setup_not_timed_out_at_2h(self):
        """Swing setup (A/B) should NOT time out at 2 hours (within 4h timeout)."""
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.setup_type = "setup_a"
        pos.created_at = int(time.time()) - 7200  # 2h — within 4h
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="open", filled=0
        ))

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "pending_entry"  # Still waiting


class TestTPHit:
    """TP fills → position fully closed."""

    def test_tp_closes_position(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.05)
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl"
        pos.tp_order_id = "ord-tp"
        monitor.register(pos)

        async def mock_fetch(order_id, pair):
            if order_id == "ord-sl":
                return make_order("ord-sl", status="open")
            if order_id == "ord-tp":
                return make_order("ord-tp", status="closed", filled=0.05, average=2080.0)
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "tp"
        # SL should be cancelled since position is fully closed
        executor.cancel_order.assert_called_once_with("ord-sl", "ETH/USDT")
        risk.on_trade_closed.assert_called_once()
        # PnL: (2080 - 2000) / 2000 = 4%
        assert abs(pos.pnl_pct - 0.04) < 0.001


class TestSLHit:
    """SL fills → cancel TP."""

    def test_sl_cancels_tp(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.05)
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl"
        pos.tp_order_id = "ord-tp"
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-sl", status="closed", filled=0.05, average=1960.0
        ))
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "sl"
        executor.cancel_order.assert_called_once_with("ord-tp", "ETH/USDT")
        risk.on_trade_closed.assert_called_once()
        # PnL: (1960 - 2000) / 2000 = -2%
        assert abs(pos.pnl_pct - (-0.02)) < 0.001


class TestBreakevenTrigger:
    """Price crosses 1:1 R:R → SL moves to entry."""

    def test_breakeven_moves_sl_to_entry(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.05)
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl-old"
        pos.tp_order_id = "ord-tp"
        pos.current_sl_price = 1960.0
        monitor.register(pos)

        # SL and TP still open
        async def mock_fetch(order_id, pair):
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)
        # Price above tp1_price (2040.0) → breakeven triggered
        executor.fetch_ticker = AsyncMock(return_value={"last": 2045.0})
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl-new"))
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.breakeven_hit is True
        assert pos.sl_order_id == "ord-sl-new"
        assert pos.current_sl_price == 2000.0  # entry price

        # New SL at entry price
        sl_call = executor.place_stop_market.call_args
        assert sl_call[0][3] == 2000.0  # breakeven price

        # Old SL cancelled
        executor.cancel_order.assert_called_once_with("ord-sl-old", "ETH/USDT")

    def test_breakeven_not_triggered_when_below_tp1(self):
        """Price below tp1_price → SL stays at original level."""
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.05)
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl"
        pos.tp_order_id = "ord-tp"
        pos.current_sl_price = 1960.0
        monitor.register(pos)

        async def mock_fetch(order_id, pair):
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)
        # Price below tp1_price (2040.0) → no breakeven
        executor.fetch_ticker = AsyncMock(return_value={"last": 2020.0})

        asyncio.run(monitor._check_all_positions())

        assert pos.breakeven_hit is False
        assert pos.current_sl_price == 1960.0
        # No SL adjustment calls
        executor.place_stop_market.assert_not_called()

    def test_breakeven_short_direction(self):
        """Short: price below tp1_price triggers breakeven."""
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(
            direction="short", phase="active", size=0.05,
            entry_price=2000.0, sl_price=2040.0
        )
        pos.tp1_price = 1960.0   # 1:1 R:R
        pos.tp2_price = 1920.0   # 2:1 R:R
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl-old"
        pos.tp_order_id = "ord-tp"
        pos.current_sl_price = 2040.0
        monitor.register(pos)

        async def mock_fetch(order_id, pair):
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)
        # Price below tp1_price (1960.0) → breakeven
        executor.fetch_ticker = AsyncMock(return_value={"last": 1955.0})
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl-new"))
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.breakeven_hit is True
        assert pos.current_sl_price == 2000.0  # entry price
        sl_call = executor.place_stop_market.call_args
        assert sl_call[0][3] == 2000.0  # breakeven

    def test_breakeven_only_triggers_once(self):
        """Once breakeven_hit=True, don't re-check."""
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.05)
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl"
        pos.tp_order_id = "ord-tp"
        pos.breakeven_hit = True  # Already triggered
        monitor.register(pos)

        async def mock_fetch(order_id, pair):
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)

        asyncio.run(monitor._check_all_positions())

        # No ticker fetch needed — breakeven already triggered
        executor.fetch_ticker.assert_not_called()


class TestTimeoutClose:
    """12h timeout → market close everything."""

    def test_duration_timeout_market_closes(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.05)
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time()) - 50000  # Way past 12h
        pos.sl_order_id = "ord-sl"
        pos.tp_order_id = "ord-tp"
        monitor.register(pos)

        executor.cancel_order = AsyncMock(return_value=True)
        executor.close_position_market = AsyncMock(return_value=make_order("ord-mkt"))

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "timeout"
        executor.close_position_market.assert_called_once()
        # SL + TP = 2 cancels
        assert executor.cancel_order.call_count == 2


class TestEmergencyClose:
    """SL placement fails after entry fill → emergency market close."""

    def test_emergency_close_on_sl_failure(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="closed", filled=0.05, average=2000.0
        ))
        executor.place_stop_market = AsyncMock(return_value=None)
        executor.close_position_market = AsyncMock(return_value=make_order("ord-emg"))

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "emergency"
        executor.close_position_market.assert_called_once_with(
            "ETH/USDT", "sell", 0.05
        )


class TestSlippage:
    """Slippage is logged on entry fill."""

    def test_slippage_calculated_correctly(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.entry_price = 2000.0
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="closed", filled=0.05, average=2001.0
        ))
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl"))
        executor.place_take_profit = AsyncMock(return_value=make_order("ord-tp"))

        with patch("execution_service.monitor.logger") as mock_logger:
            asyncio.run(monitor._check_all_positions())
            slippage_calls = [
                c for c in mock_logger.info.call_args_list
                if "Slippage" in str(c)
            ]
            assert len(slippage_calls) >= 1


class TestPnlCalculation:
    """PnL is calculated correctly for longs and shorts."""

    def test_long_profit(self):
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="long", phase="active")
        pos.actual_entry_price = 2000.0
        monitor._calculate_pnl(pos, 2080.0)
        assert abs(pos.pnl_pct - 0.04) < 0.0001

    def test_long_loss(self):
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="long", phase="active")
        pos.actual_entry_price = 2000.0
        monitor._calculate_pnl(pos, 1960.0)
        assert abs(pos.pnl_pct - (-0.02)) < 0.0001

    def test_short_profit(self):
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="short", phase="active")
        pos.actual_entry_price = 2000.0
        monitor._calculate_pnl(pos, 1920.0)
        assert abs(pos.pnl_pct - 0.04) < 0.0001

    def test_short_loss(self):
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="short", phase="active")
        pos.actual_entry_price = 2000.0
        monitor._calculate_pnl(pos, 2040.0)
        assert abs(pos.pnl_pct - (-0.02)) < 0.0001


class TestAdjustSLFailure:
    """If new SL placement fails, old SL is kept."""

    def test_adjust_sl_failure_keeps_old(self):
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.05)
        pos.actual_entry_price = 2000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl-old"
        pos.tp_order_id = "ord-tp"
        monitor.register(pos)

        # New SL placement fails
        executor.place_stop_market = AsyncMock(return_value=None)
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._adjust_sl(pos, 2000.0))

        # Old SL should be kept
        assert pos.sl_order_id == "ord-sl-old"
        # Old SL should NOT be cancelled
        executor.cancel_order.assert_not_called()


class TestSLTPValidation:
    """SL/TP price ordering validation (live mode only)."""

    def test_long_invalid_sl_above_entry_rejected(self):
        """Long: SL > entry should be rejected in live mode."""
        risk = MagicMock()
        executor = _mock_executor()
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        service = _make_service(executor, monitor, risk)

        setup = make_setup(sl=2050.0, entry=2000.0, tp2=2080.0)
        with patch.object(settings, "OKX_SANDBOX", False):
            result = asyncio.run(service.execute(setup, make_approval(), 0.85))
        assert result is False

    def test_short_invalid_sl_below_entry_rejected(self):
        """Short: SL < entry should be rejected in live mode."""
        risk = MagicMock()
        executor = _mock_executor()
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        service = _make_service(executor, monitor, risk)

        setup = make_setup(
            direction="short", sl=1950.0, entry=2000.0,
            tp1=1960.0, tp2=1920.0, tp3=1880.0
        )
        with patch.object(settings, "OKX_SANDBOX", False):
            result = asyncio.run(service.execute(setup, make_approval(), 0.85))
        assert result is False


class TestTPPlacementFailure:
    """TP placement fails → position stays open with SL only."""

    def test_tp_failure_keeps_position_with_sl(self):
        """TP fails but position stays active — SL protects us."""
        executor = _mock_executor()
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="closed", filled=0.05, average=2001.0
        ))
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl"))
        executor.place_take_profit = AsyncMock(return_value=None)  # TP fails

        asyncio.run(monitor._check_all_positions())

        # Position stays active with SL protection
        assert pos.phase == "active"
        assert pos.sl_order_id == "ord-sl"
        assert pos.tp_order_id is None


class TestAlgoOrderFetch:
    """Algo order fetching via OKX native API."""

    def test_algo_order_pending(self):
        """Pending algo order returns open status."""
        executor = OrderExecutor.__new__(OrderExecutor)
        executor._exchange = MagicMock()
        executor._algo_fetch_errors = {}

        executor._exchange.privateGetTradeOrdersAlgoPending = MagicMock(
            return_value={"data": [{"algoId": "algo-123", "sz": "0.1"}]}
        )

        async def run():
            return await executor._fetch_algo_order("algo-123", "ETH/USDT")

        result = asyncio.run(run())
        assert result is not None
        assert result["status"] == "open"
        assert result["id"] == "algo-123"

    def test_algo_order_filled(self):
        """Filled algo order returns closed status with fill data."""
        executor = OrderExecutor.__new__(OrderExecutor)
        executor._exchange = MagicMock()
        executor._algo_fetch_errors = {}

        executor._exchange.privateGetTradeOrdersAlgoPending = MagicMock(
            return_value={"data": []}
        )
        executor._exchange.privateGetTradeOrdersAlgoHistory = MagicMock(
            return_value={"data": [
                {"algoId": "algo-456", "sz": "0.5", "avgPx": "2100.5"}
            ]}
        )

        async def run():
            return await executor._fetch_algo_order("algo-456", "ETH/USDT")

        result = asyncio.run(run())
        assert result is not None
        assert result["status"] == "closed"
        assert result["filled"] == 0.5
        assert result["average"] == 2100.5

    def test_algo_order_cancelled(self):
        """Cancelled algo order returns canceled status."""
        executor = OrderExecutor.__new__(OrderExecutor)
        executor._exchange = MagicMock()
        executor._algo_fetch_errors = {}

        executor._exchange.privateGetTradeOrdersAlgoPending = MagicMock(
            return_value={"data": []}
        )
        executor._exchange.privateGetTradeOrdersAlgoHistory = MagicMock(
            side_effect=[
                {"data": []},
                {"data": [{"algoId": "algo-789"}]},
            ]
        )

        async def run():
            return await executor._fetch_algo_order("algo-789", "ETH/USDT")

        result = asyncio.run(run())
        assert result is not None
        assert result["status"] == "canceled"


class TestHealthCheck:
    """Health method returns service status."""

    def test_health_returns_status(self):
        service = _make_service()
        service._monitor.positions = {
            "ETH/USDT": MagicMock(phase="active", direction="long")
        }
        health = service.health()
        assert health["enabled"] is True
        assert health["active_positions"] == 1
