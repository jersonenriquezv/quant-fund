"""
Tests for Execution Service (Layer 5).

All ccxt calls are mocked. Tests verify business logic only:
- Happy path: entry placed, position registered
- Disabled without API key
- Entry fill → SL/TP placement
- Entry timeout → cancel
- TP1 fill → SL moves to breakeven
- TP2 fill → SL moves to TP1
- SL fill → cancel all TPs
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
    pair="BTC/USDT",
    direction="long",
    entry=50000.0,
    sl=49500.0,
    tp1=50500.0,
    tp2=51000.0,
    tp3=51500.0,
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


def make_approval(size=0.002, leverage=3.0) -> RiskApproval:
    return RiskApproval(
        approved=True,
        position_size=size,
        leverage=leverage,
        risk_pct=0.02,
        reason="All checks passed",
    )


def make_order(order_id="ord-123", status="open", filled=0, average=0, price=50000.0):
    return {
        "id": order_id,
        "status": status,
        "filled": filled,
        "average": average,
        "price": price,
    }


def _make_service(executor=None, monitor=None, risk=None):
    """Create an ExecutionService with injected mocks (bypass __init__)."""
    service = ExecutionService.__new__(ExecutionService)
    service._enabled = True
    service._executor = executor or MagicMock(spec=OrderExecutor)
    service._monitor = monitor or MagicMock(spec=PositionMonitor)
    service._risk = risk or MagicMock()
    return service


def make_position(
    pair="BTC/USDT",
    direction="long",
    phase="pending_entry",
    entry_price=50000.0,
    sl_price=49500.0,
    size=0.002,
) -> ManagedPosition:
    return ManagedPosition(
        pair=pair,
        direction=direction,
        setup_type="setup_a",
        phase=phase,
        entry_price=entry_price,
        sl_price=sl_price,
        tp1_price=50500.0,
        tp2_price=51000.0,
        tp3_price=51500.0,
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
        executor = MagicMock(spec=OrderExecutor)
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}

        executor.configure_pair = AsyncMock(return_value=True)
        executor.fetch_ticker = AsyncMock(return_value={"ask": 50000.0, "bid": 49990.0})
        executor.place_limit_order = AsyncMock(return_value=make_order("ord-1"))

        service = _make_service(executor, monitor, risk)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))

        assert result is True
        executor.configure_pair.assert_called_once_with("BTC/USDT", 3)
        # Sandbox: limit order at ask * 1.0005 (buy with 0.05% tolerance)
        executor.place_limit_order.assert_called_once()
        call_args = executor.place_limit_order.call_args
        assert call_args[0][0] == "BTC/USDT"  # pair
        assert call_args[0][1] == "buy"        # side
        assert call_args[0][2] == 0.002         # size
        assert call_args[0][3] == pytest.approx(50000.0 * 1.0005, rel=1e-6)  # price
        monitor.register.assert_called_once()
        risk.on_trade_opened.assert_called_once()

    def test_short_uses_sell_side(self):
        risk = MagicMock()
        executor = MagicMock(spec=OrderExecutor)
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}

        executor.configure_pair = AsyncMock(return_value=True)
        executor.fetch_ticker = AsyncMock(return_value={"ask": 50010.0, "bid": 50000.0})
        executor.place_limit_order = AsyncMock(return_value=make_order("ord-2"))

        service = _make_service(executor, monitor, risk)

        setup = make_setup(
            direction="short", entry=50000.0, sl=50500.0,
            tp1=49500.0, tp2=49000.0, tp3=48500.0
        )
        result = asyncio.run(
            service.execute(setup, make_approval(), 0.70)
        )

        assert result is True
        call_args = executor.place_limit_order.call_args
        assert call_args[0][1] == "sell"  # side
        assert call_args[0][3] == pytest.approx(50000.0 * 0.9995, rel=1e-6)  # bid - tolerance

    def test_skips_if_pair_has_active_position(self):
        """Active (filled) positions block new entries for the same pair."""
        monitor = MagicMock(spec=PositionMonitor)
        active_pos = MagicMock()
        active_pos.phase = "active"
        monitor.positions = {"BTC/USDT": active_pos}

        service = _make_service(monitor=monitor)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))
        assert result is False

    def test_replaces_pending_entry_with_new_setup(self):
        """Pending (unfilled) entries are cancelled and replaced by new setups."""
        risk = MagicMock()
        executor = MagicMock(spec=OrderExecutor)
        monitor = MagicMock(spec=PositionMonitor)

        # Existing pending position
        old_pos = MagicMock()
        old_pos.phase = "pending_entry"
        old_pos.direction = "short"
        old_pos.entry_price = 2108.26
        monitor.positions = {"ETH/USDT": old_pos}

        # cancel_and_remove_pending returns the old position, then positions is empty
        monitor.cancel_and_remove_pending = AsyncMock(return_value=old_pos)
        # After cancel, positions dict is empty for the new entry check
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
        executor = MagicMock(spec=OrderExecutor)
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        executor.configure_pair = AsyncMock(return_value=False)

        service = _make_service(executor, monitor)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))
        assert result is False

    def test_returns_false_on_order_failure(self):
        executor = MagicMock(spec=OrderExecutor)
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        executor.configure_pair = AsyncMock(return_value=True)
        executor.fetch_ticker = AsyncMock(return_value={"ask": 50000.0, "bid": 49990.0})
        executor.place_limit_order = AsyncMock(return_value=None)

        service = _make_service(executor, monitor)

        result = asyncio.run(service.execute(make_setup(), make_approval(), 0.85))
        assert result is False


# ================================================================
# PositionMonitor — State machine tests
# ================================================================

class TestEntryFill:
    """Entry order fills → SL + TP placed."""

    def test_entry_fill_places_sl_and_tps(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="closed", filled=0.002, average=50010.0
        ))
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl"))
        executor.place_take_profit = AsyncMock(side_effect=[
            make_order("ord-tp1"),
            make_order("ord-tp2"),
            make_order("ord-tp3"),
        ])

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "active"
        assert pos.actual_entry_price == 50010.0
        assert pos.filled_size == 0.002
        assert pos.sl_order_id == "ord-sl"
        assert pos.tp1_order_id == "ord-tp1"
        assert pos.tp2_order_id == "ord-tp2"
        assert pos.tp3_order_id == "ord-tp3"


class TestEntryTimeout:
    """Entry not filled within timeout → cancel."""

    def test_entry_timeout_cancels_order(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.created_at = int(time.time()) - 15000  # Well past 4h
        monitor.register(pos)

        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        executor.cancel_order.assert_called_once_with("ord-entry", "BTC/USDT")
        assert pos.phase == "closed"
        assert pos.close_reason == "cancelled"
        # Cancelled entries are not real trades — on_trade_closed NOT called
        risk.on_trade_closed.assert_not_called()
        # But on_trade_cancelled IS called to remove phantom from risk state
        risk.on_trade_cancelled.assert_called_once_with("BTC/USDT", "long")

    def test_quick_setup_uses_shorter_timeout(self):
        """Quick setups (C/D/E) use ENTRY_TIMEOUT_QUICK_SECONDS."""
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.setup_type = "setup_c"
        # 2 hours ago — past 1h quick timeout but within 4h swing timeout
        pos.created_at = int(time.time()) - 7200
        monitor.register(pos)

        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "cancelled"

    def test_swing_setup_not_timed_out_at_2h(self):
        """Swing setup (A/B) should NOT time out at 2 hours (within 4h timeout)."""
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.setup_type = "setup_a"
        # 2 hours ago — within 4h swing timeout
        pos.created_at = int(time.time()) - 7200
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="open", filled=0
        ))

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "pending_entry"  # Still waiting


class TestTP1Hit:
    """TP1 fills → SL moves to breakeven."""

    def test_tp1_moves_sl_to_breakeven(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.002)
        pos.actual_entry_price = 50000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl-old"
        pos.tp1_order_id = "ord-tp1"
        pos.tp2_order_id = "ord-tp2"
        pos.tp3_order_id = "ord-tp3"
        monitor.register(pos)

        sl_order = make_order("ord-sl-old", status="open")
        tp1_order = make_order("ord-tp1", status="closed", filled=0.001)

        async def mock_fetch(order_id, pair):
            if order_id == "ord-sl-old":
                return sl_order
            if order_id == "ord-tp1":
                return tp1_order
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl-new"))
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "tp1_hit"
        executor.place_stop_market.assert_called_once()
        call_args = executor.place_stop_market.call_args
        assert call_args[0][3] == 50000.0  # breakeven price
        executor.cancel_order.assert_called_once_with("ord-sl-old", "BTC/USDT")
        assert pos.sl_order_id == "ord-sl-new"


class TestTP2Hit:
    """TP2 fills → SL moves to TP1 level."""

    def test_tp2_moves_sl_to_tp1(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="tp1_hit", size=0.002)
        pos.actual_entry_price = 50000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl-be"
        pos.tp2_order_id = "ord-tp2"
        pos.tp3_order_id = "ord-tp3"
        monitor.register(pos)

        async def mock_fetch(order_id, pair):
            if order_id == "ord-sl-be":
                return make_order("ord-sl-be", status="open")
            if order_id == "ord-tp2":
                return make_order("ord-tp2", status="closed", filled=0.0006)
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)
        executor.place_stop_market = AsyncMock(return_value=make_order("ord-sl-tp1"))
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "tp2_hit"
        call_args = executor.place_stop_market.call_args
        assert call_args[0][3] == 50500.0  # TP1 price
        assert pos.sl_order_id == "ord-sl-tp1"


class TestSLHit:
    """SL fills → cancel all TPs."""

    def test_sl_cancels_all_tps(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.002)
        pos.actual_entry_price = 50000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl"
        pos.tp1_order_id = "ord-tp1"
        pos.tp2_order_id = "ord-tp2"
        pos.tp3_order_id = "ord-tp3"
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-sl", status="closed", filled=0.002, average=49500.0
        ))
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "sl"
        assert executor.cancel_order.call_count == 3
        risk.on_trade_closed.assert_called_once()


class TestTimeoutClose:
    """12h timeout → market close everything."""

    def test_duration_timeout_market_closes(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.002)
        pos.actual_entry_price = 50000.0
        pos.filled_at = int(time.time()) - 50000  # Way past 12h
        pos.sl_order_id = "ord-sl"
        pos.tp1_order_id = "ord-tp1"
        pos.tp2_order_id = "ord-tp2"
        pos.tp3_order_id = "ord-tp3"
        monitor.register(pos)

        executor.cancel_order = AsyncMock(return_value=True)
        executor.close_position_market = AsyncMock(return_value=make_order("ord-mkt"))

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "timeout"
        executor.close_position_market.assert_called_once()
        # SL + 3 TPs = 4 cancels
        assert executor.cancel_order.call_count == 4


class TestEmergencyClose:
    """SL placement fails after entry fill → emergency market close."""

    def test_emergency_close_on_sl_failure(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="closed", filled=0.002, average=50000.0
        ))
        executor.place_stop_market = AsyncMock(return_value=None)
        executor.close_position_market = AsyncMock(return_value=make_order("ord-emg"))

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "emergency"
        executor.close_position_market.assert_called_once_with(
            "BTC/USDT", "sell", 0.002
        )


class TestSlippage:
    """Slippage is logged on entry fill."""

    def test_slippage_calculated_correctly(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position()
        pos.entry_price = 50000.0
        monitor.register(pos)

        executor.fetch_order = AsyncMock(return_value=make_order(
            "ord-entry", status="closed", filled=0.002, average=50025.0
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
        pos.actual_entry_price = 50000.0
        monitor._calculate_pnl(pos, 51000.0)
        assert abs(pos.pnl_pct - 0.02) < 0.0001

    def test_long_loss(self):
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="long", phase="active")
        pos.actual_entry_price = 50000.0
        monitor._calculate_pnl(pos, 49500.0)
        assert abs(pos.pnl_pct - (-0.01)) < 0.0001

    def test_short_profit(self):
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="short", phase="active")
        pos.actual_entry_price = 50000.0
        monitor._calculate_pnl(pos, 49000.0)
        assert abs(pos.pnl_pct - 0.02) < 0.0001

    def test_short_loss(self):
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="short", phase="active")
        pos.actual_entry_price = 50000.0
        monitor._calculate_pnl(pos, 50500.0)
        assert abs(pos.pnl_pct - (-0.01)) < 0.0001

    def test_blended_pnl_with_realized(self):
        """PnL blends realized from TPs with unrealized remainder."""
        monitor = PositionMonitor(MagicMock(), MagicMock())
        pos = make_position(direction="long", phase="tp1_hit")
        pos.actual_entry_price = 50000.0
        # TP1 realized: 50% at 50500 = 0.001 * 500 = $0.50
        pos.realized_pnl_usd = 0.50
        # Remaining 50% at SL (breakeven 50000) = $0
        monitor._calculate_pnl(pos, 50000.0)
        # Total: $0.50 / (50000 * 0.002) = 0.005
        assert abs(pos.pnl_pct - 0.005) < 0.0001


class TestTP3FullClose:
    """TP3 fills → position fully closed."""

    def test_tp3_fill_closes_position(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="tp2_hit", size=0.002)
        pos.actual_entry_price = 50000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl-tp1"
        pos.tp3_order_id = "ord-tp3"
        monitor.register(pos)

        async def mock_fetch(order_id, pair):
            if order_id == "ord-sl-tp1":
                return make_order("ord-sl-tp1", status="open")
            if order_id == "ord-tp3":
                return make_order("ord-tp3", status="closed", filled=0.0004)
            return make_order(order_id, status="open")

        executor.fetch_order = AsyncMock(side_effect=mock_fetch)
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._check_all_positions())

        assert pos.phase == "closed"
        assert pos.close_reason == "tp3"
        # SL should be cancelled since position is fully closed
        executor.cancel_order.assert_called_once_with("ord-sl-tp1", "BTC/USDT")
        risk.on_trade_closed.assert_called_once()


class TestAdjustSLFailure:
    """If new SL placement fails, old SL is kept."""

    def test_adjust_sl_failure_keeps_old(self):
        executor = MagicMock(spec=OrderExecutor)
        risk = MagicMock()
        monitor = PositionMonitor(executor, risk)

        pos = make_position(phase="active", size=0.002)
        pos.actual_entry_price = 50000.0
        pos.filled_at = int(time.time())
        pos.sl_order_id = "ord-sl-old"
        pos.tp1_order_id = "ord-tp1"
        pos.tp2_order_id = "ord-tp2"
        pos.tp3_order_id = "ord-tp3"
        monitor.register(pos)

        # New SL placement fails
        executor.place_stop_market = AsyncMock(return_value=None)
        executor.cancel_order = AsyncMock(return_value=True)

        asyncio.run(monitor._adjust_sl(pos, 50000.0))

        # Old SL should be kept
        assert pos.sl_order_id == "ord-sl-old"
        # Old SL should NOT be cancelled
        executor.cancel_order.assert_not_called()


class TestSLTPValidation:
    """I-E4: SL/TP price ordering validation (live mode only)."""

    def test_long_invalid_sl_above_entry_rejected(self):
        """Long: SL > entry should be rejected in live mode."""
        risk = MagicMock()
        executor = MagicMock(spec=OrderExecutor)
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        service = _make_service(executor, monitor, risk)

        setup = make_setup(sl=50500.0, entry=50000.0)  # SL above entry
        with patch.object(settings, "OKX_SANDBOX", False):
            result = asyncio.run(service.execute(setup, make_approval(), 0.85))
        assert result is False

    def test_short_invalid_sl_below_entry_rejected(self):
        """Short: SL < entry should be rejected in live mode."""
        risk = MagicMock()
        executor = MagicMock(spec=OrderExecutor)
        monitor = MagicMock(spec=PositionMonitor)
        monitor.positions = {}
        service = _make_service(executor, monitor, risk)

        setup = make_setup(
            direction="short", sl=49500.0, entry=50000.0,
            tp1=49000.0, tp2=48000.0, tp3=47000.0
        )
        with patch.object(settings, "OKX_SANDBOX", False):
            result = asyncio.run(service.execute(setup, make_approval(), 0.85))
        assert result is False


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
        # First history call (effective) returns empty
        # Second history call (canceled) finds the order
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

    def test_algo_order_error_throttling(self):
        """Repeated errors are throttled — first logged, then every 12th."""
        executor = OrderExecutor.__new__(OrderExecutor)
        executor._exchange = MagicMock()
        executor._algo_fetch_errors = {}

        executor._exchange.privateGetTradeOrdersAlgoPending = MagicMock(
            side_effect=Exception("API error")
        )

        async def run():
            results = []
            for _ in range(25):
                r = await executor._fetch_algo_order("algo-err", "ETH/USDT")
                results.append(r)
            return results

        results = asyncio.run(run())
        # All return None
        assert all(r is None for r in results)
        # Error counter tracks
        assert executor._algo_fetch_errors["algo-err"] == 25


class TestHealthCheck:
    """Health method returns service status."""

    def test_health_returns_status(self):
        service = _make_service()
        service._monitor.positions = {
            "BTC/USDT": MagicMock(phase="active", direction="long")
        }
        health = service.health()
        assert health["enabled"] is True
        assert health["active_positions"] == 1
