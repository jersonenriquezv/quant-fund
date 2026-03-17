"""Tests for risk_service.state_tracker — trade lifecycle, DD, cooldown, date reset, Redis persistence."""

import time
import pytest
from unittest.mock import patch
from datetime import datetime, date, timedelta, timezone
from risk_service.state_tracker import RiskStateTracker


class FakeRedis:
    """In-memory fake that mimics RedisStore.set_bot_state/get_bot_state."""

    def __init__(self):
        self._data: dict[str, str] = {}

    def set_bot_state(self, key_name: str, value: str, ttl: int = 86400) -> None:
        self._data[key_name] = value

    def get_bot_state(self, key_name: str) -> str | None:
        return self._data.get(key_name)


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
        tracker.record_trade_opened("BTC/USDT", "long", 50000, int(time.time()), phase="active")
        assert tracker.get_open_positions_count() == 1

    def test_pending_position_counts_toward_max(self, tracker):
        """Pending entries count toward max positions to prevent unlimited orders."""
        tracker.record_trade_opened("BTC/USDT", "long", 50000, int(time.time()))
        assert tracker.get_open_positions_count() == 1  # pending counts

    def test_pending_to_active_via_filled(self, tracker):
        """record_trade_filled promotes pending → active, count stays 1."""
        tracker.record_trade_opened("BTC/USDT", "long", 50000, int(time.time()))
        assert tracker.get_open_positions_count() == 1
        tracker.record_trade_filled("BTC/USDT", "long")
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
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now, phase="active")
        tracker.record_trade_opened("ETH/USDT", "short", 3000, now, phase="active")
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


class TestTradeCancelled:
    """record_trade_cancelled removes from open positions without PnL impact."""

    def test_cancel_removes_from_open_positions(self, tracker):
        now = int(time.time())
        tracker.record_trade_opened("ETH/USDT", "short", 2108, now, phase="active")
        assert tracker.get_open_positions_count() == 1

        tracker.record_trade_cancelled("ETH/USDT", "short")
        assert tracker.get_open_positions_count() == 0

    def test_cancel_does_not_count_as_trade(self, tracker):
        now = int(time.time())
        tracker.record_trade_opened("ETH/USDT", "short", 2108, now)
        tracker.record_trade_cancelled("ETH/USDT", "short")
        assert tracker.get_trades_today_count() == 0

    def test_cancel_does_not_affect_pnl(self, tracker):
        now = int(time.time())
        tracker.record_trade_opened("ETH/USDT", "short", 2108, now)
        tracker.record_trade_cancelled("ETH/USDT", "short")
        assert tracker.get_daily_dd_pct() == 0.0
        assert tracker.get_weekly_dd_pct() == 0.0

    def test_cancel_nonexistent_is_safe(self, tracker):
        """Cancelling a pair that's not open should not crash."""
        tracker.record_trade_cancelled("BTC/USDT", "long")
        assert tracker.get_open_positions_count() == 0


class TestDirectionMatching:
    """I-R1: Position close matches by (pair, direction)."""

    def test_close_matches_correct_direction(self, tracker):
        """With same pair in both directions, close the right one."""
        now = int(time.time())
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now, phase="active")
        tracker.record_trade_opened("BTC/USDT", "short", 50000, now, phase="active")
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
        tracker.record_trade_opened("BTC/USDT", "long", 50000, now, phase="active")

        # Try to close a short that doesn't exist
        tracker.record_trade_closed("BTC/USDT", "short", -0.01, now + 3600)
        # Long should still be open
        assert tracker.get_open_positions_count() == 1


# ============================================================
# Redis persistence
# ============================================================

class TestRedisPersistence:

    def test_round_trip_daily_pnl(self):
        """State survives a simulated restart via shared Redis."""
        redis = FakeRedis()
        t1 = RiskStateTracker(capital=1000.0, redis_store=redis)
        now = int(time.time())
        t1.record_trade_closed("BTC/USDT", "long", -0.02, now)
        assert t1.get_daily_dd_pct() == pytest.approx(0.02)

        # Simulate restart — new tracker loads from same Redis
        t2 = RiskStateTracker(capital=1000.0, redis_store=redis)
        assert t2.get_daily_dd_pct() == pytest.approx(0.02)

    def test_round_trip_weekly_pnl(self):
        redis = FakeRedis()
        t1 = RiskStateTracker(capital=1000.0, redis_store=redis)
        now = int(time.time())
        t1.record_trade_closed("BTC/USDT", "long", -0.03, now)

        t2 = RiskStateTracker(capital=1000.0, redis_store=redis)
        assert t2.get_weekly_dd_pct() == pytest.approx(0.03)

    def test_round_trip_trades_today_count(self):
        redis = FakeRedis()
        t1 = RiskStateTracker(capital=1000.0, redis_store=redis)
        now = int(time.time())
        t1.record_trade_opened("BTC/USDT", "long", 50000, now)
        t1.record_trade_closed("BTC/USDT", "long", 0.01, now + 100)
        t1.record_trade_opened("ETH/USDT", "short", 3000, now + 200)
        t1.record_trade_closed("ETH/USDT", "short", -0.01, now + 300)
        assert t1.get_trades_today_count() == 2

        t2 = RiskStateTracker(capital=1000.0, redis_store=redis)
        assert t2.get_trades_today_count() == 2

    def test_round_trip_cooldown(self):
        redis = FakeRedis()
        t1 = RiskStateTracker(capital=1000.0, redis_store=redis)
        now = int(time.time())
        t1.record_trade_closed("BTC/USDT", "long", -0.01, now)
        assert t1.get_last_loss_time() == now

        t2 = RiskStateTracker(capital=1000.0, redis_store=redis)
        assert t2.get_last_loss_time() == now

    def test_no_redis_works_fine(self):
        """Tracker without Redis works exactly as before."""
        t = RiskStateTracker(capital=1000.0, redis_store=None)
        now = int(time.time())
        t.record_trade_closed("BTC/USDT", "long", -0.02, now)
        assert t.get_daily_dd_pct() == pytest.approx(0.02)

    def test_stale_day_not_restored(self):
        """If saved state is from yesterday, daily values reset."""
        redis = FakeRedis()
        t1 = RiskStateTracker(capital=1000.0, redis_store=redis)
        now = int(time.time())
        t1.record_trade_closed("BTC/USDT", "long", -0.02, now)

        # Tamper with Redis to simulate "saved yesterday"
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        redis.set_bot_state("risk_state_day", yesterday)

        t2 = RiskStateTracker(capital=1000.0, redis_store=redis)
        # Daily should NOT be restored (different day)
        assert t2.get_daily_dd_pct() == 0.0
        assert t2.get_trades_today_count() == 0

    def test_stale_week_not_restored(self):
        """If saved state is from a different week, weekly values reset."""
        redis = FakeRedis()
        t1 = RiskStateTracker(capital=1000.0, redis_store=redis)
        now = int(time.time())
        t1.record_trade_closed("BTC/USDT", "long", -0.03, now)

        # Tamper with Redis to simulate different week
        current_week = datetime.now(timezone.utc).isocalendar()[1]
        redis.set_bot_state("risk_state_week", str(current_week - 1))

        t2 = RiskStateTracker(capital=1000.0, redis_store=redis)
        assert t2.get_weekly_dd_pct() == 0.0

    def test_redis_failure_on_load_starts_fresh(self):
        """If Redis raises on load, tracker starts fresh without crashing."""
        class BrokenRedis:
            def get_bot_state(self, key):
                raise ConnectionError("Redis down")
            def set_bot_state(self, key, value, ttl=0):
                raise ConnectionError("Redis down")

        t = RiskStateTracker(capital=1000.0, redis_store=BrokenRedis())
        assert t.get_daily_dd_pct() == 0.0
        # Should not crash on save either
        t.record_trade_closed("BTC/USDT", "long", -0.01, int(time.time()))
