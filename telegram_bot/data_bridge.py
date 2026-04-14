"""Read-only adapter — gathers data from live services for display.

No business logic here. Just reads from the injected services and returns
dicts ready for the formatters.
"""

import json
import time
from typing import Any, Callable

from config.settings import settings
from shared.logger import setup_logger

logger = setup_logger("telegram_bot")


class DataBridge:
    """Reads from live services to build display data for Telegram handlers."""

    def __init__(
        self,
        data_service,
        strategy_service,
        risk_service,
        execution_service,
        shadow_monitor,
        get_last_setup_time: Callable[[], float],
        bot_start_time: float,
    ) -> None:
        self._ds = data_service
        self._ss = strategy_service
        self._rs = risk_service
        self._es = execution_service
        self._sm = shadow_monitor
        self._get_last_setup_time = get_last_setup_time
        self._bot_start_time = bot_start_time

    def get_portfolio(self) -> dict[str, Any]:
        """Portfolio summary: capital, P&L, open positions with unrealized P&L."""
        state = self._rs._state
        capital = state.get_capital()

        positions = self._get_positions_with_pnl()

        return {
            "capital": capital,
            "daily_pnl_pct": state._daily_pnl_pct,
            "weekly_pnl_pct": state._weekly_pnl_pct,
            "trades_today": state.get_trades_today_count(),
            "heat_usd": state.get_portfolio_heat_usd(),
            "positions": positions,
        }

    def get_market(self) -> dict[str, Any]:
        """Market overview: prices, funding rates, Fear & Greed."""
        prices = []
        funding = []

        for pair in settings.TRADING_PAIRS:
            candle = self._ds.get_latest_candle(pair, "5m")
            price = candle.close if candle else 0.0

            # 24h change from daily candle
            change_24h = None
            daily = self._ds.get_latest_candle(pair, "1D")
            if daily and daily.open > 0:
                change_24h = ((price - daily.open) / daily.open) * 100

            prices.append({
                "pair": pair,
                "price": price,
                "change_24h": change_24h,
            })

            fr = self._ds.get_funding_rate(pair)
            if fr:
                funding.append({"pair": pair, "rate": fr.rate})

        # Fear & Greed from Redis
        fear_greed = None
        try:
            fg_raw = self._ds.redis.get_bot_state("news:fear_greed")
            if fg_raw:
                fg = json.loads(fg_raw)
                fear_greed = {"score": fg.get("score", "?"), "label": fg.get("label", "?")}
        except Exception:
            pass

        return {
            "prices": prices,
            "funding": funding,
            "fear_greed": fear_greed,
        }

    def get_positions(self) -> list[dict[str, Any]]:
        """Detailed open positions with unrealized P&L."""
        return self._get_positions_with_pnl()

    def get_bot_status(self) -> dict[str, Any]:
        """Bot status: uptime, data state, shadow mode, last setup."""
        now = time.time()

        # Shadow monitor stats
        shadow_count = 0
        shadow_filled = 0
        if self._sm is not None:
            shadow_count = self._sm.active_count
            shadow_filled = sum(
                1 for sp in self._sm._positions.values() if sp.filled
            )

        # Open positions count
        open_count = 0
        if self._es and self._es._monitor:
            open_count = sum(
                1 for p in self._es._monitor.positions.values()
                if p.phase in ("pending_entry", "active")
            )

        # Data service state
        data_state = "unknown"
        if self._ds:
            data_state = self._ds.state.name if hasattr(self._ds, "state") else "running"

        last_setup = self._get_last_setup_time()

        return {
            "uptime_seconds": now - self._bot_start_time,
            "data_state": data_state,
            "last_setup_time": last_setup if last_setup > 0 else None,
            "shadow_count": shadow_count,
            "shadow_filled": shadow_filled,
            "open_positions": open_count,
        }

    def get_recent_trades(self, page: int = 0, per_page: int = 5) -> tuple[list[dict], bool]:
        """Fetch recent closed trades with pagination. Returns (trades, has_next)."""
        if not self._ds or not self._ds.postgres:
            return [], False

        # Fetch one extra to check if there's a next page
        all_trades = self._ds.postgres.fetch_recent_closed_trades(
            limit=per_page * (page + 1) + 1,
        )
        start = page * per_page
        end = start + per_page
        trades = all_trades[start:end]
        has_next = len(all_trades) > end

        return trades, has_next

    def get_active_obs(self, pair_filter: str | None = None) -> list[dict[str, Any]]:
        """Active order blocks with distance from current price."""
        pairs = [pair_filter] if pair_filter and pair_filter != "all" else settings.TRADING_PAIRS

        # Read from Redis (published by main._publish_strategy_state)
        obs_list = []
        try:
            raw = self._ds.redis.get_bot_state("order_blocks")
            if raw:
                all_obs = json.loads(raw)
            else:
                all_obs = []

            bias_raw = self._ds.redis.get_bot_state("htf_bias")
            biases = json.loads(bias_raw) if bias_raw else {}
        except Exception:
            return []

        for ob in all_obs:
            pair = ob.get("pair", "")
            if pair not in pairs:
                continue

            candle = self._ds.get_latest_candle(pair, "5m")
            current_price = candle.close if candle else 0.0

            distance_pct = 0.0
            if current_price > 0:
                distance_pct = (ob["entry_price"] - current_price) / current_price * 100

            obs_list.append({
                "pair": pair,
                "timeframe": ob.get("timeframe", "?"),
                "direction": ob.get("direction", "?"),
                "entry_price": ob["entry_price"],
                "volume_ratio": ob.get("volume_ratio", 0),
                "distance_pct": distance_pct,
                "current_price": current_price,
                "htf_bias": biases.get(pair, "undefined"),
            })

        # Sort by absolute distance
        obs_list.sort(key=lambda x: abs(x["distance_pct"]))
        return obs_list

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_positions_with_pnl(self) -> list[dict[str, Any]]:
        """Build position list with unrealized P&L from live prices."""
        if not self._es or not self._es._monitor:
            return []

        result = []
        for pair, pos in self._es._monitor.positions.items():
            if pos.phase not in ("pending_entry", "active"):
                continue

            entry = pos.actual_entry_price or pos.entry_price
            candle = self._ds.get_latest_candle(pair, "5m")
            current = candle.close if candle else entry

            if pos.direction == "long":
                pnl_pct = (current - entry) / entry if entry > 0 else 0
            else:
                pnl_pct = (entry - current) / entry if entry > 0 else 0

            pnl_usd = pnl_pct * (pos.filled_size or 0) * entry

            result.append({
                "pair": pair,
                "direction": pos.direction,
                "entry": entry,
                "sl": pos.sl_price,
                "tp": pos.tp2_price,
                "size": pos.filled_size or 0,
                "leverage": pos.leverage,
                "phase": pos.phase,
                "filled_at": getattr(pos, "filled_at", None),
                "unrealized_pnl_pct": pnl_pct,
                "unrealized_pnl_usd": pnl_usd,
            })

        return result
