"""
In-memory risk state tracker.

Tracks trades opened today, open positions, daily/weekly P&L, cooldown timer.
Public methods let Execution Service (future) update state on trade open/close.
Auto-resets daily counters at midnight UTC and weekly counters on Monday UTC.
"""

import time
from datetime import datetime, timezone


class RiskStateTracker:
    """Tracks live trading state for risk checks."""

    def __init__(self, capital: float) -> None:
        self._capital = capital

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

    # ================================================================
    # Trade lifecycle
    # ================================================================

    def record_trade_opened(
        self, pair: str, direction: str, entry_price: float, timestamp: int
    ) -> None:
        """Record a new position opened."""
        self._check_date_reset()
        self._open_positions.append({
            "pair": pair,
            "direction": direction,
            "entry_price": entry_price,
            "timestamp": timestamp,
        })

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

        if today != self._current_day:
            self._trades_today.clear()
            self._daily_pnl_pct = 0.0
            self._current_day = today

        if week != self._current_week:
            self._weekly_pnl_pct = 0.0
            self._current_week = week
