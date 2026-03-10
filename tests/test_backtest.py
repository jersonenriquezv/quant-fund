"""
Tests for the backtester TradeSimulator.

Covers: SL hit, TP1 partial + breakeven, full TP3, timeout,
entry not filled, position sizing with leverage cap.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import patch

from shared.models import Candle, TradeSetup
from config.settings import settings
from scripts.backtest import (
    TradeSimulator, SimulatedTrade, compute_metrics, _compute_max_drawdown,
)


# ================================================================
# Helpers
# ================================================================

def _candle(pair: str = "BTC/USDT", timestamp: int = 1000000,
            open: float = 50000, high: float = 50100,
            low: float = 49900, close: float = 50050,
            timeframe: str = "5m") -> Candle:
    return Candle(
        timestamp=timestamp, open=open, high=high, low=low, close=close,
        volume=10.0, volume_quote=500000.0, pair=pair, timeframe=timeframe,
        confirmed=True,
    )


def _setup(pair: str = "BTC/USDT", direction: str = "long",
           entry: float = 49500, sl: float = 49000,
           tp1: float = 50000, tp2: float = 50500,
           setup_type: str = "setup_a", timestamp: int = 1000000) -> TradeSetup:
    return TradeSetup(
        timestamp=timestamp, pair=pair, direction=direction,
        setup_type=setup_type, entry_price=entry, sl_price=sl,
        tp1_price=tp1, tp2_price=tp2,
        confluences=["ob", "sweep"], htf_bias="bullish",
        ob_timeframe="15m",
    )


# ================================================================
# Entry fill tests
# ================================================================

class TestEntryFill:
    def test_long_entry_fills_when_price_drops(self):
        """Long limit order fills when candle low <= entry price."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500)
        candle0 = _candle(timestamp=1000000, close=50000)

        sim.on_setup(setup, candle0)
        assert len(sim.pending) == 1

        # Candle that drops to entry
        candle1 = _candle(timestamp=2000000, low=49400, high=50000)
        sim.on_candle(candle1)
        assert len(sim.pending) == 0
        assert len(sim.active) == 1
        assert sim.active[0].entry_time_ms == 2000000

    def test_short_entry_fills_when_price_rises(self):
        """Short limit order fills when candle high >= entry price."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="short", entry=50500, sl=51000,
                       tp1=50000, tp2=49500)
        candle0 = _candle(timestamp=1000000, close=50000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, high=50600, low=50000)
        sim.on_candle(candle1)
        assert len(sim.active) == 1

    def test_entry_not_filled_price_doesnt_reach(self):
        """Entry stays pending if price doesn't reach entry level."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        # Candle that doesn't reach entry
        candle1 = _candle(timestamp=2000000, low=49600, high=50200)
        sim.on_candle(candle1)
        assert len(sim.pending) == 1
        assert len(sim.active) == 0

    @patch("scripts.backtest.settings")
    def test_entry_timeout(self, mock_settings):
        """Pending entry expires after ENTRY_TIMEOUT_SECONDS."""
        mock_settings.ENTRY_TIMEOUT_SECONDS = 14400  # 4h
        mock_settings.ENTRY_TIMEOUT_QUICK_SECONDS = 3600
        mock_settings.MAX_OPEN_POSITIONS = 5
        mock_settings.RISK_PER_TRADE = 0.02
        mock_settings.MAX_LEVERAGE = 5
        mock_settings.TP1_CLOSE_PCT = 0.50
        mock_settings.TP2_CLOSE_PCT = 0.30
        mock_settings.MAX_TRADE_DURATION_SECONDS = 43200
        mock_settings.MIN_RISK_DISTANCE_PCT = 0.001
        mock_settings.MIN_RISK_REWARD = 1.5
        mock_settings.MIN_RISK_REWARD_QUICK = 1.0
        mock_settings.COOLDOWN_MINUTES = 30
        mock_settings.MAX_TRADES_PER_DAY = 100
        mock_settings.MAX_DAILY_DRAWDOWN = 0.10
        mock_settings.MAX_WEEKLY_DRAWDOWN = 0.20

        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, timestamp=1000000)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        # Entry timeout: 4h = 14,400,000 ms
        deadline = 1000000 + 14400 * 1000
        candle_late = _candle(timestamp=deadline + 1, low=49600)
        sim.on_candle(candle_late)

        assert len(sim.pending) == 0
        assert len(sim.active) == 0
        assert len(sim.closed) == 1
        assert sim.closed[0].exit_reason == "entry_timeout"


# ================================================================
# SL hit tests
# ================================================================

class TestStopLoss:
    def test_long_sl_hit(self):
        """Long trade: SL triggers when candle low <= sl_price."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, sl=49000)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        # Fill entry (high stays below TP1=50000 to avoid partial close)
        candle1 = _candle(timestamp=2000000, low=49400, high=49700)
        sim.on_candle(candle1)
        assert len(sim.active) == 1

        # SL hit
        candle2 = _candle(timestamp=3000000, low=48900, high=49600)
        sim.on_candle(candle2)

        assert len(sim.active) == 0
        assert len(sim.get_closed_trades()) == 1
        trade = sim.get_closed_trades()[0]
        assert trade.exit_reason == "sl"
        assert trade.pnl_usd < 0  # Loss

    def test_short_sl_hit(self):
        """Short trade: SL triggers when candle high >= sl_price."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="short", entry=50500, sl=51000,
                       tp1=50000, tp2=49500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        # Fill short entry (low stays above TP1=50000 to avoid partial close)
        candle1 = _candle(timestamp=2000000, high=50600, low=50100)
        sim.on_candle(candle1)
        assert len(sim.active) == 1

        candle2 = _candle(timestamp=3000000, high=51100, low=50400)
        sim.on_candle(candle2)

        trade = sim.get_closed_trades()[0]
        assert trade.exit_reason == "sl"
        assert trade.pnl_usd < 0

    def test_sl_priority_over_tp(self):
        """If SL and TP could both hit on same candle, SL wins."""
        sim = TradeSimulator(initial_capital=10000)
        # Long: entry=49500, sl=49000, tp1=50000
        setup = _setup(direction="long", entry=49500, sl=49000, tp1=50000)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        # Fill entry with high below TP1
        candle1 = _candle(timestamp=2000000, low=49400, high=49700)
        sim.on_candle(candle1)

        # Both SL and TP1 reachable in same candle — SL checked first
        candle2 = _candle(timestamp=3000000, low=48900, high=50100)
        sim.on_candle(candle2)

        trade = sim.get_closed_trades()[0]
        assert trade.exit_reason == "sl"  # SL has priority


# ================================================================
# Take profit tests
# ================================================================

class TestTakeProfit:
    def test_breakeven_triggers_at_tp1(self):
        """Price crosses tp1 → SL moves to breakeven (entry price)."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, sl=49000,
                       tp1=50000, tp2=50500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49400)
        sim.on_candle(candle1)

        # TP1 hit — breakeven triggered, trade still active
        candle2 = _candle(timestamp=3000000, high=50100, low=49600)
        sim.on_candle(candle2)

        assert len(sim.active) == 1
        trade = sim.active[0]
        assert trade.breakeven_hit is True
        assert trade.current_sl == trade.entry_price  # Breakeven

    def test_breakeven_sl_closes_at_entry(self):
        """After breakeven, SL at entry → PnL ~= 0."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, sl=49000,
                       tp1=50000, tp2=50500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49400)
        sim.on_candle(candle1)

        # TP1 hit → breakeven
        candle2 = _candle(timestamp=3000000, high=50100, low=49600)
        sim.on_candle(candle2)

        # Price drops back to entry (breakeven SL)
        candle3 = _candle(timestamp=4000000, low=49400, high=49600)
        sim.on_candle(candle3)

        trades = sim.get_closed_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "breakeven_sl"
        assert trade.pnl_usd == pytest.approx(0.0, abs=0.01)

    def test_trailing_sl_moves_to_tp1(self):
        """Price crosses midpoint(tp1,tp2) → SL moves to tp1."""
        sim = TradeSimulator(initial_capital=10000)
        # entry=49500, sl=49000, tp1=50000, tp2=50500
        # midpoint = (50000 + 50500) / 2 = 50250
        setup = _setup(direction="long", entry=49500, sl=49000,
                       tp1=50000, tp2=50500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49400)
        sim.on_candle(candle1)

        # TP1 hit → breakeven
        candle2 = _candle(timestamp=3000000, high=50100, low=49600)
        sim.on_candle(candle2)

        # Midpoint hit → trailing SL to tp1
        candle3 = _candle(timestamp=4000000, high=50300, low=49800)
        sim.on_candle(candle3)

        assert len(sim.active) == 1
        trade = sim.active[0]
        assert trade.trailing_sl_moved is True
        assert trade.current_sl == trade.tp1_price  # 50000

    def test_trailing_sl_exit(self):
        """After trailing SL set to tp1, price drops → exits at tp1."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, sl=49000,
                       tp1=50000, tp2=50500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49400)
        sim.on_candle(candle1)

        # TP1 hit → breakeven
        candle2 = _candle(timestamp=3000000, high=50100, low=49600)
        sim.on_candle(candle2)

        # Midpoint hit → trailing SL to tp1
        candle3 = _candle(timestamp=4000000, high=50300, low=49800)
        sim.on_candle(candle3)

        # Price drops to tp1 (trailing SL)
        candle4 = _candle(timestamp=5000000, low=49900, high=50100)
        sim.on_candle(candle4)

        trades = sim.get_closed_trades()
        assert len(trades) == 1
        assert trades[0].exit_reason == "trailing_sl"
        assert trades[0].exit_price == 50000  # tp1
        assert trades[0].pnl_usd > 0  # Profit (exit at tp1 > entry)

    def test_full_tp_exit(self):
        """Price reaches tp2 → 100% close with full profit."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, sl=49000,
                       tp1=50000, tp2=50500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49400)
        sim.on_candle(candle1)

        # TP2 hit — full close
        candle2 = _candle(timestamp=3000000, high=50600, low=49600)
        sim.on_candle(candle2)

        trades = sim.get_closed_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.exit_reason == "tp"
        assert trade.exit_price == 50500
        assert trade.pnl_usd > 0

    def test_large_candle_hits_tp_directly(self):
        """Large candle covers tp2 directly → closes at tp2."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, sl=49000,
                       tp1=50000, tp2=50500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49400)
        sim.on_candle(candle1)

        # Huge candle covers all levels
        candle2 = _candle(timestamp=3000000, low=49600, high=51200)
        sim.on_candle(candle2)

        trades = sim.get_closed_trades()
        assert len(trades) == 1
        assert trades[0].exit_reason == "tp"
        assert trades[0].exit_price == 50500


# ================================================================
# Timeout tests
# ================================================================

class TestTimeout:
    @patch("scripts.backtest.settings")
    def test_trade_timeout(self, mock_settings):
        """Trade closes at candle.close after MAX_TRADE_DURATION_SECONDS."""
        mock_settings.MAX_TRADE_DURATION_SECONDS = 43200  # 12h
        mock_settings.MAX_TRADE_DURATION_QUICK = 14400
        mock_settings.MAX_OPEN_POSITIONS = 5
        mock_settings.RISK_PER_TRADE = 0.02
        mock_settings.MAX_LEVERAGE = 5
        mock_settings.ENTRY_TIMEOUT_SECONDS = 14400
        mock_settings.ENTRY_TIMEOUT_QUICK_SECONDS = 3600
        mock_settings.TP1_CLOSE_PCT = 0.50
        mock_settings.TP2_CLOSE_PCT = 0.30
        mock_settings.MIN_RISK_DISTANCE_PCT = 0.001
        mock_settings.MIN_RISK_REWARD = 1.5
        mock_settings.MIN_RISK_REWARD_QUICK = 1.0
        mock_settings.COOLDOWN_MINUTES = 30
        mock_settings.MAX_TRADES_PER_DAY = 100
        mock_settings.MAX_DAILY_DRAWDOWN = 0.10
        mock_settings.MAX_WEEKLY_DRAWDOWN = 0.20

        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="long", entry=49500, sl=49000,
                       tp1=50000, tp2=50500, timestamp=1000000)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49400)
        sim.on_candle(candle1)

        # 12h later
        timeout_ts = 2000000 + 43200 * 1000
        candle_late = _candle(timestamp=timeout_ts, close=49800,
                              low=49600, high=50000)
        sim.on_candle(candle_late)

        trades = sim.get_closed_trades()
        assert len(trades) == 1
        assert trades[0].exit_reason == "timeout"


# ================================================================
# Position sizing tests
# ================================================================

class TestPositionSizing:
    def test_basic_sizing(self):
        """Position size = (capital * risk%) / |entry - sl|."""
        sim = TradeSimulator(initial_capital=10000)
        # entry=50000, sl=49500 → distance=$500
        # risk = 10000 * 0.02 = $200
        # size = 200 / 500 = 0.4 BTC
        setup = _setup(direction="long", entry=50000, sl=49500,
                       tp1=50500, tp2=51000)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        trade = sim.pending[0]

        expected_size = (10000 * 0.02) / 500  # 0.4
        assert trade.position_size == pytest.approx(expected_size, rel=0.01)

    def test_leverage_cap(self):
        """Leverage capped at MAX_LEVERAGE when risk-based size is too large."""
        sim = TradeSimulator(initial_capital=1000)
        # entry=50000, sl=49850 → distance=$150 (0.3%, passes MIN_RISK_DISTANCE_PCT)
        # risk = 1000 * 0.02 = $20
        # size = 20 / 150 = 0.133 BTC → notional = 6,667 → leverage = 6.67x
        # Capped: leverage=MAX → notional=MAX*1000 → size=MAX*1000/50000
        setup = _setup(direction="long", entry=50000, sl=49850,
                       tp1=50150, tp2=50300)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        trade = sim.pending[0]

        assert trade.leverage == pytest.approx(settings.MAX_LEVERAGE)
        expected_size = (1000 * settings.MAX_LEVERAGE) / 50000
        assert trade.position_size == pytest.approx(expected_size, rel=0.01)

    def test_max_open_positions(self):
        """Rejects setup when MAX_OPEN_POSITIONS reached."""
        sim = TradeSimulator(initial_capital=10000)
        candle0 = _candle(timestamp=1000000)

        for i in range(settings.MAX_OPEN_POSITIONS):
            setup = _setup(entry=49500 - i * 100, sl=49000 - i * 100,
                           timestamp=1000000 + i)
            assert sim.on_setup(setup, candle0) is True

        # Next one should be rejected
        extra = _setup(entry=48000, sl=47500, timestamp=1000000 + 100)
        assert sim.on_setup(extra, candle0) is False


# ================================================================
# PnL computation tests
# ================================================================

class TestPnLComputation:
    def test_sl_loss_pnl(self):
        """SL hit produces expected loss amount."""
        sim = TradeSimulator(initial_capital=10000)
        # entry=50000, sl=49500, distance=$500
        # risk = 10000 * 0.02 = $200, size = 0.4 BTC
        # SL loss = (49500 - 50000) * 0.4 = -$200
        setup = _setup(direction="long", entry=50000, sl=49500,
                       tp1=50500, tp2=51000)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, low=49900)
        sim.on_candle(candle1)
        candle2 = _candle(timestamp=3000000, low=49400)
        sim.on_candle(candle2)

        trade = sim.get_closed_trades()[0]
        expected_loss = -200.0  # 2% of 10000
        assert trade.pnl_usd == pytest.approx(expected_loss, rel=0.01)

    def test_equity_updates_after_trades(self):
        """Equity reflects cumulative PnL from trades."""
        sim = TradeSimulator(initial_capital=10000)

        # Trade 1: SL hit → lose $200
        setup1 = _setup(direction="long", entry=50000, sl=49500,
                        tp1=50500, tp2=51000, timestamp=1000000)
        candle0 = _candle(timestamp=1000000)
        sim.on_setup(setup1, candle0)
        sim.on_candle(_candle(timestamp=2000000, low=49900))  # Fill
        sim.on_candle(_candle(timestamp=3000000, low=49400))  # SL

        assert sim.equity == pytest.approx(9800, rel=0.01)


# ================================================================
# Metrics tests
# ================================================================

class TestMetrics:
    def test_max_drawdown(self):
        """Max drawdown computed correctly from equity curve."""
        curve = [
            (0, 10000),
            (1, 10200),   # Peak
            (2, 9800),    # Trough: DD = (10200-9800)/10200 = 3.92%
            (3, 10100),
        ]
        dd = _compute_max_drawdown(curve)
        expected = (10200 - 9800) / 10200 * 100
        assert dd == pytest.approx(expected, rel=0.01)

    def test_max_drawdown_monotonic_up(self):
        """No drawdown if equity only goes up."""
        curve = [(0, 10000), (1, 10100), (2, 10200)]
        assert _compute_max_drawdown(curve) == 0.0

    def test_metrics_with_no_trades(self):
        """Metrics handle zero trades gracefully."""
        sim = TradeSimulator(initial_capital=10000)
        m = compute_metrics(sim, period_days=30)
        assert m.total_trades == 0
        assert m.win_rate == 0.0
        assert m.total_pnl_usd == 0.0


# ================================================================
# Short trade tests
# ================================================================

class TestShortTrades:
    def test_short_breakeven_and_tp(self):
        """Short trade: breakeven triggers at tp1, then TP hit at tp2."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="short", entry=50500, sl=51000,
                       tp1=50000, tp2=49500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        # Fill short entry (price rises to entry)
        candle1 = _candle(timestamp=2000000, high=50600, low=50000)
        sim.on_candle(candle1)

        # TP1 hit (price drops to tp1) → breakeven
        candle2 = _candle(timestamp=3000000, low=49900, high=50400)
        sim.on_candle(candle2)
        assert sim.active[0].breakeven_hit is True
        assert sim.active[0].current_sl == 50500  # Breakeven

        # TP2 hit → full close
        candle3 = _candle(timestamp=4000000, low=49400, high=50100)
        sim.on_candle(candle3)

        trades = sim.get_closed_trades()
        assert len(trades) == 1
        assert trades[0].exit_reason == "tp"
        assert trades[0].pnl_usd > 0

    def test_short_breakeven_sl_exit(self):
        """Short trade: breakeven SL triggered after tp1 cross."""
        sim = TradeSimulator(initial_capital=10000)
        setup = _setup(direction="short", entry=50500, sl=51000,
                       tp1=50000, tp2=49500)
        candle0 = _candle(timestamp=1000000)

        sim.on_setup(setup, candle0)
        candle1 = _candle(timestamp=2000000, high=50600, low=50000)
        sim.on_candle(candle1)

        # TP1 hit → breakeven
        candle2 = _candle(timestamp=3000000, low=49900, high=50400)
        sim.on_candle(candle2)

        # Price goes back up to breakeven SL
        candle3 = _candle(timestamp=4000000, high=50600, low=50100)
        sim.on_candle(candle3)

        trades = sim.get_closed_trades()
        assert len(trades) == 1
        assert trades[0].exit_reason == "breakeven_sl"
        assert trades[0].pnl_usd == pytest.approx(0.0, abs=0.01)
