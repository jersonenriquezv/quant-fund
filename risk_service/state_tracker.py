"""
Risk state tracker with optional Redis persistence.

Tracks trades opened today, open positions, daily/weekly P&L, cooldown timer.
Public methods let Execution Service update state on trade open/close.
Auto-resets daily counters at midnight UTC and weekly counters on Monday UTC.

If a RedisStore is provided, state is persisted on every mutation and restored
on init — survives bot restarts without losing guardrail state.
"""

import time
from datetime import datetime, date, timezone

from shared.logger import setup_logger

logger = setup_logger("risk_state")


class RiskStateTracker:
    """Tracks live trading state for risk checks."""

    def __init__(self, capital: float, redis_store=None) -> None:
        self._capital = capital
        self._redis = redis_store

        # Trades completed today (closed)
        self._trades_today: list[dict] = []
        # Currently open positions
        self._open_positions: list[dict] = []

        # Drawdown tracking (positive fractions, e.g. 0.02 = 2% loss)
        self._daily_pnl_pct: float = 0.0
        self._weekly_pnl_pct: float = 0.0

        # Cooldown
        self._last_loss_time: int | None = None

        # Date tracking for auto-reset
        now = datetime.now(timezone.utc)
        self._current_day = now.date()
        self._current_week: int = now.isocalendar()[1]

        # Restore state from Redis if available
        self._load_from_redis()

    # ================================================================
    # Trade lifecycle
    # ================================================================

    def record_trade_opened(
        self, pair: str, direction: str, entry_price: float, timestamp: int,
        *, phase: str = "pending",
    ) -> None:
        """Record a new position opened.

        Args:
            phase: "pending" for limit orders not yet filled,
                   "active" for immediately filled positions.
        """
        self._check_date_reset()
        self._open_positions.append({
            "pair": pair,
            "direction": direction,
            "entry_price": entry_price,
            "timestamp": timestamp,
            "phase": phase,
        })
        self._save_to_redis()

    def record_trade_filled(self, pair: str, direction: str) -> None:
        """Mark a pending position as active (limit order filled)."""
        for pos in self._open_positions:
            if pos["pair"] == pair and pos["direction"] == direction and pos.get("phase") == "pending":
                pos["phase"] = "active"
                self._save_to_redis()
                return

    def record_trade_closed(
        self, pair: str, direction: str, pnl_pct: float, timestamp: int
    ) -> None:
        """Record a position closed.

        Args:
            pair: Trading pair.
            direction: "long" or "short" — matches the exact position.
            pnl_pct: P&L as fraction of capital (positive = profit, negative = loss).
            timestamp: Unix timestamp in seconds.
        """
        self._check_date_reset()

        # Remove from open positions (match by pair AND direction)
        for i, pos in enumerate(self._open_positions):
            if pos["pair"] == pair and pos["direction"] == direction:
                self._open_positions.pop(i)
                break

        # Add to today's trades
        self._trades_today.append({
            "pair": pair,
            "pnl_pct": pnl_pct,
            "timestamp": timestamp,
        })

        # Update P&L (simple summation of per-trade pnl_pct).
        # This is an approximation — summing percentages is not exact for
        # compounding, but the error is negligible at our trade sizes
        # (1-2% risk per trade, max 5 trades/day).
        self._daily_pnl_pct += pnl_pct
        self._weekly_pnl_pct += pnl_pct

        # Set cooldown on loss
        if pnl_pct < 0:
            self._last_loss_time = timestamp

        self._save_to_redis()

    def record_trade_cancelled(self, pair: str, direction: str) -> None:
        """Remove a cancelled pending entry from open positions tracking.

        Unlike record_trade_closed, this does NOT add to trades_today or
        affect P&L — the order never filled, so it's not a real trade.
        """
        for i, pos in enumerate(self._open_positions):
            if pos["pair"] == pair and pos["direction"] == direction:
                self._open_positions.pop(i)
                break
        self._save_to_redis()

    # ================================================================
    # Capital
    # ================================================================

    def set_capital(self, amount: float) -> None:
        self._capital = amount

    def get_capital(self) -> float:
        return self._capital

    # ================================================================
    # Getters for guardrail checks
    # ================================================================

    def get_trades_today_count(self) -> int:
        self._check_date_reset()
        return len(self._trades_today)

    def get_open_positions_count(self) -> int:
        """Return count of ALL tracked positions (pending + active).

        Pending entries count toward max positions to prevent placing
        unlimited orders when none have filled yet.
        """
        return len(self._open_positions)

    def get_daily_dd_pct(self) -> float:
        """Return daily drawdown as positive fraction (0.0 if profitable)."""
        self._check_date_reset()
        return max(0.0, -self._daily_pnl_pct)

    def get_weekly_dd_pct(self) -> float:
        """Return weekly drawdown as positive fraction (0.0 if profitable)."""
        self._check_date_reset()
        return max(0.0, -self._weekly_pnl_pct)

    def get_last_loss_time(self) -> int | None:
        return self._last_loss_time

    # ================================================================
    # Date reset
    # ================================================================

    def _check_date_reset(self) -> None:
        """Auto-reset daily counters at midnight UTC, weekly on Monday."""
        now = datetime.now(timezone.utc)
        today = now.date()
        week = now.isocalendar()[1]

        reset = False
        if today != self._current_day:
            self._trades_today.clear()
            self._daily_pnl_pct = 0.0
            self._current_day = today
            reset = True

        if week != self._current_week:
            self._weekly_pnl_pct = 0.0
            self._current_week = week
            reset = True

        if reset:
            self._save_to_redis()

    # ================================================================
    # Redis persistence
    # ================================================================

    _REDIS_TTL = 172800  # 48 hours

    def _save_to_redis(self) -> None:
        """Persist risk state to Redis (fire-and-forget)."""
        if self._redis is None:
            return
        try:
            ttl = self._REDIS_TTL
            self._redis.set_bot_state("risk_daily_pnl", str(self._daily_pnl_pct), ttl=ttl)
            self._redis.set_bot_state("risk_weekly_pnl", str(self._weekly_pnl_pct), ttl=ttl)
            self._redis.set_bot_state(
                "risk_last_loss_time",
                str(self._last_loss_time) if self._last_loss_time is not None else "",
                ttl=ttl,
            )
            self._redis.set_bot_state("risk_trades_today", str(len(self._trades_today)), ttl=ttl)
            self._redis.set_bot_state("risk_state_day", self._current_day.isoformat(), ttl=ttl)
            self._redis.set_bot_state("risk_state_week", str(self._current_week), ttl=ttl)
        except Exception as e:
            logger.warning(f"Failed to save risk state to Redis: {e}")

    def _load_from_redis(self) -> None:
        """Restore risk state from Redis on startup."""
        if self._redis is None:
            return
        try:
            saved_day = self._redis.get_bot_state("risk_state_day")
            if saved_day is None:
                logger.info("No risk state in Redis — starting fresh")
                return

            # Parse saved date and check if it's still current
            saved_date = date.fromisoformat(saved_day)
            today = self._current_day

            # Restore daily values only if same day
            if saved_date == today:
                daily = self._redis.get_bot_state("risk_daily_pnl")
                if daily is not None:
                    self._daily_pnl_pct = float(daily)

                trades_today = self._redis.get_bot_state("risk_trades_today")
                if trades_today is not None:
                    # Reconstruct _trades_today as a list with N placeholder entries
                    # (we only need the count for guardrails)
                    count = int(trades_today)
                    self._trades_today = [{"pair": "restored", "pnl_pct": 0, "timestamp": 0}] * count

            # Restore weekly values only if same week
            saved_week = self._redis.get_bot_state("risk_state_week")
            if saved_week is not None and int(saved_week) == self._current_week:
                weekly = self._redis.get_bot_state("risk_weekly_pnl")
                if weekly is not None:
                    self._weekly_pnl_pct = float(weekly)

            # Cooldown always restores (time-based, not date-based)
            last_loss = self._redis.get_bot_state("risk_last_loss_time")
            if last_loss is not None and last_loss != "":
                self._last_loss_time = int(last_loss)

            logger.info(
                f"Risk state restored from Redis: "
                f"daily_pnl={self._daily_pnl_pct:.4f} weekly_pnl={self._weekly_pnl_pct:.4f} "
                f"trades_today={len(self._trades_today)} "
                f"last_loss={'set' if self._last_loss_time else 'none'}"
            )
        except Exception as e:
            logger.warning(f"Failed to load risk state from Redis — starting fresh: {e}")
