"""Tests for risk_service.state_tracker — trade lifecycle, DD, cooldown, date reset."""

import time
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from risk_service.state_tracker import RiskStateTracker


@pytest.fixture
def tracker():
    return RiskStateTracker(capital=1000.0)


# ============================================================
# Capital
# ============================================================

class TestCapital:

    def test_initial_capital(self, tracker):
        assert tracker.get_capital() == 1000.0

    def test_set_capital(self, tracker):
        tracker.set_capital(5000.0)
        assert tracker.get_capital() == 5000.0


# ============================================================
# Trade open/close
# ============================================================

class TestTradeLifecycle:

    def test_open_position_increments_count(self, tracker):
        assert tracker.get_open_positions_count() == 0
        tracker.record_trade_opened("BTC/USDT", "long", 50000, int(time.time()))
        assert tracker.get_open_positions_count() == 1

    def test_close_position_decrements_count(self, tracker):
        now = int(time.time())
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now)
        tracker.record_trade_closed("BTC/USDT", "long",0.01, now + 3600)
        assert tracker.get_open_positions_count() == 0

    def test_close_increments_trades_today(self, tracker):
        now = int(time.time())
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now)
        assert tracker.get_trades_today_count() == 0
        tracker.record_trade_closed("BTC/USDT", "long",0.01, now + 3600)
        assert tracker.get_trades_today_count() == 1

    def test_multiple_positions(self, tracker):
        now = int(time.time())
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now)
        tracker.record_trade_opened("ETH/USDT", "short", 3000, now)
        assert tracker.get_open_positions_count() == 2

        tracker.record_trade_closed("BTC/USDT", "long",0.01, now + 3600)
        assert tracker.get_open_positions_count() == 1

    def test_close_nonexistent_pair_safe(self, tracker):
        """Closing a pair that's not open should not crash."""
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.01, now)
        assert tracker.get_trades_today_count() == 1
        assert tracker.get_open_positions_count() == 0


# ============================================================
# Drawdown tracking
# ============================================================

class TestDrawdown:

    def test_initial_dd_zero(self, tracker):
        assert tracker.get_daily_dd_pct() == 0.0
        assert tracker.get_weekly_dd_pct() == 0.0

    def test_loss_increases_dd(self, tracker):
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.02, now)
        assert tracker.get_daily_dd_pct() == pytest.approx(0.02)
        assert tracker.get_weekly_dd_pct() == pytest.approx(0.02)

    def test_profit_reduces_dd(self, tracker):
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.02, now)
        tracker.record_trade_closed("ETH/USDT", "short",0.01, now + 100)
        # net = -0.01
        assert tracker.get_daily_dd_pct() == pytest.approx(0.01)

    def test_profit_only_dd_is_zero(self, tracker):
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",0.03, now)
        assert tracker.get_daily_dd_pct() == 0.0
        assert tracker.get_weekly_dd_pct() == 0.0

    def test_multiple_losses_accumulate(self, tracker):
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.01, now)
        tracker.record_trade_closed("ETH/USDT", "short",-0.015, now + 100)
        assert tracker.get_daily_dd_pct() == pytest.approx(0.025)
        assert tracker.get_weekly_dd_pct() == pytest.approx(0.025)


# ============================================================
# Cooldown
# ============================================================

class TestCooldown:

    def test_initial_no_cooldown(self, tracker):
        assert tracker.get_last_loss_time() is None

    def test_loss_sets_cooldown(self, tracker):
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.01, now)
        assert tracker.get_last_loss_time() == now

    def test_profit_does_not_set_cooldown(self, tracker):
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",0.01, now)
        assert tracker.get_last_loss_time() is None

    def test_cooldown_updates_on_new_loss(self, tracker):
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.01, now)
        tracker.record_trade_closed("ETH/USDT", "short",-0.01, now + 600)
        assert tracker.get_last_loss_time() == now + 600


# ============================================================
# Date reset
# ============================================================

class TestDateReset:

    def test_daily_reset_clears_trades_and_dd(self, tracker):
        """Simulate crossing midnight by patching datetime."""
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.02, now)

        assert tracker.get_trades_today_count() == 1
        assert tracker.get_daily_dd_pct() == pytest.approx(0.02)

        # Fake a new day by changing the tracker's current_day
        tracker._current_day = tracker._current_day - timedelta(days=1)

        # Next access triggers reset
        assert tracker.get_trades_today_count() == 0
        assert tracker.get_daily_dd_pct() == 0.0

    def test_weekly_reset_clears_weekly_dd(self, tracker):
        """Simulate crossing Monday by changing current_week."""
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.03, now)

        assert tracker.get_weekly_dd_pct() == pytest.approx(0.03)

        # Fake a new week
        tracker._current_week = tracker._current_week - 1

        assert tracker.get_weekly_dd_pct() == 0.0

    def test_daily_reset_preserves_weekly(self, tracker):
        """Daily reset should NOT clear weekly P&L."""
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.02, now)

        # Fake daily reset only
        tracker._current_day = tracker._current_day - timedelta(days=1)

        assert tracker.get_daily_dd_pct() == 0.0
        assert tracker.get_weekly_dd_pct() == pytest.approx(0.02)

    def test_cooldown_persists_across_daily_reset(self, tracker):
        """Cooldown is NOT cleared by daily reset (it's time-based)."""
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long",-0.01, now)

        tracker._current_day = tracker._current_day - timedelta(days=1)

        assert tracker.get_last_loss_time() == now

    def test_year_boundary_daily_reset(self, tracker):
        """Daily reset works correctly across year boundary (Dec 31 → Jan 1)."""
        from datetime import date
        now = int(time.time())
        tracker.record_trade_closed("BTC/USDT", "long", -0.02, now)

        assert tracker.get_trades_today_count() == 1
        assert tracker.get_daily_dd_pct() == pytest.approx(0.02)

        # Simulate crossing year boundary: Dec 31 → Jan 1
        tracker._current_day = date(2025, 12, 31)

        # Next access triggers reset (date() comparison works across years)
        assert tracker.get_trades_today_count() == 0
        assert tracker.get_daily_dd_pct() == 0.0


class TestDirectionMatching:
    """I-R1: Position close matches by (pair, direction)."""

    def test_close_matches_correct_direction(self, tracker):
        """With same pair in both directions, close the right one."""
        now = int(time.time())
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now)
        tracker.record_trade_opened("BTC/USDT", "short", 50000, now)
        assert tracker.get_open_positions_count() == 2

        # Close only the long
        tracker.record_trade_closed("BTC/USDT", "long", 0.01, now + 3600)
        assert tracker.get_open_positions_count() == 1

        # Remaining should be the short
        remaining = tracker._open_positions[0]
        assert remaining["direction"] == "short"

    def test_close_wrong_direction_does_not_remove(self, tracker):
        """Closing a direction that doesn't exist should not remove anything."""
        now = int(time.time())
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now)

        # Try to close a short that doesn't exist
        tracker.record_trade_closed("BTC/USDT", "short", -0.01, now + 3600)
        # Long should still be open
        assert tracker.get_open_positions_count() == 1
