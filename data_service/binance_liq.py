"""
Binance Futures WebSocket — liquidation data only.

Connects to wss://fstream.binance.com/ws/!forceOrder@arr
to receive real-time liquidation events across all pairs.

We only use Binance for liquidation data, NOT for trading or market data.
BTC/ETH liquidations are correlated across exchanges — Binance has
the highest volume so its liquidation stream is the best proxy.

No API key required. Public WebSocket.
"""

import asyncio
import json
import time
from collections import defaultdict

import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import settings
from shared.logger import setup_logger
from shared.models import LiquidationEvent

logger = setup_logger("data_service")

_BINANCE_FUTURES_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"

# Only process liquidations for our pairs
_SYMBOL_TO_PAIR = {
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
}

# Aggregation window in seconds
_AGGREGATION_WINDOW_SEC = 300  # 5 minutes


class BinanceLiquidationFeed:
    """Binance Futures WebSocket for real-time liquidation events.

    Stores individual events and provides aggregated queries.
    Other services call get_recent_liquidations() directly.
    """

    def __init__(self):
        # All liquidation events, pruned to last hour
        self._events: list[LiquidationEvent] = []

        # Connection state
        self._ws = None
        self._running = False
        self._connected = False
        self._reconnect_delay = settings.RECONNECT_INITIAL_DELAY
        self._last_message_time = 0.0

        # Stats for logging
        self._events_received = 0
        self._events_filtered = 0

    # ================================================================
    # Public interface
    # ================================================================

    def get_recent_liquidations(self, pair: str | None = None,
                                minutes: int = 60) -> list[LiquidationEvent]:
        """Get liquidation events from the last N minutes.

        Args:
            pair: Filter by pair ("BTC/USDT") or None for all pairs.
            minutes: How far back to look (default 60 min).

        Returns:
            List of LiquidationEvent sorted by timestamp ascending.
        """
        cutoff = int((time.time() - minutes * 60) * 1000)
        events = [e for e in self._events if e.timestamp >= cutoff]
        if pair:
            events = [e for e in events if e.pair == pair]
        return events

    def get_aggregated_stats(self, pair: str,
                             minutes: int = 5) -> dict:
        """Get aggregated liquidation stats for a pair over N minutes.

        Returns dict with total_usd, long_usd, short_usd, count.
        This is what the Strategy Service uses to evaluate sweep strength.
        """
        events = self.get_recent_liquidations(pair, minutes)
        long_usd = sum(e.size_usd for e in events if e.side == "long")
        short_usd = sum(e.size_usd for e in events if e.side == "short")
        return {
            "total_usd": long_usd + short_usd,
            "long_usd": long_usd,
            "short_usd": short_usd,
            "count": len(events),
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ================================================================
    # WebSocket lifecycle
    # ================================================================

    async def start(self) -> None:
        """Start Binance liquidation WebSocket with auto-reconnection."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                self._connected = False
                if not self._running:
                    break
                logger.warning(f"Binance liquidation WS disconnected. Reason: {e}")
                await self._reconnect_backoff()

    async def stop(self) -> None:
        """Gracefully stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("Binance liquidation WS stopped gracefully")

    # ================================================================
    # Internal: connect and listen
    # ================================================================

    async def _connect_and_listen(self) -> None:
        """Connect to Binance and listen for forceOrder events."""
        logger.info(f"Binance liquidation WS connecting to {_BINANCE_FUTURES_WS}")

        async with websockets.connect(
            _BINANCE_FUTURES_WS,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = settings.RECONNECT_INITIAL_DELAY
            self._last_message_time = time.time()

            logger.info("Binance liquidation WS connected — listening for forceOrder events")

            async for raw_msg in ws:
                self._last_message_time = time.time()

                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    logger.warning(f"Binance WS: non-JSON message: {raw_msg[:200]}")
                    continue

                self._handle_force_order(msg)

    def _handle_force_order(self, msg: dict) -> None:
        """Parse Binance forceOrder message.

        Format:
        {
            "e": "forceOrder",
            "E": 1234567890123,
            "o": {
                "s": "BTCUSDT",      # symbol
                "S": "SELL",         # side — SELL = long liquidated, BUY = short liquidated
                "q": "0.014",        # quantity
                "p": "9910",         # price
                "ap": "9910",        # average price
                "X": "FILLED",      # status
                "l": "0.014",        # last filled quantity
                "z": "0.014",        # cumulative filled quantity
                "T": 1234567890123   # trade time
            }
        }
        """
        self._events_received += 1

        event_type = msg.get("e")
        if event_type != "forceOrder":
            return

        order = msg.get("o", {})
        symbol = order.get("s", "")

        # Filter: only our pairs
        pair = _SYMBOL_TO_PAIR.get(symbol)
        if not pair:
            self._events_filtered += 1
            return

        try:
            side_raw = order.get("S", "")
            quantity = float(order.get("q", 0))
            price = float(order.get("p", 0))
            trade_time = int(order.get("T", 0))

            # SELL = long was liquidated, BUY = short was liquidated
            side = "long" if side_raw == "SELL" else "short"
            size_usd = quantity * price

            if price <= 0 or quantity <= 0:
                logger.warning(f"Binance liquidation invalid data: pair={pair} "
                               f"price={price} qty={quantity}")
                return

            event = LiquidationEvent(
                timestamp=trade_time,
                pair=pair,
                side=side,
                size_usd=size_usd,
                price=price,
                source="binance_forceOrder",
            )

            self._events.append(event)
            self._prune_old_events()

            logger.info(f"Liquidation: pair={pair} side={side} "
                        f"size=${size_usd:,.0f} price={price:,.2f}")

        except (ValueError, KeyError) as e:
            logger.error(f"Binance liquidation parse error: msg={msg} error={e}")

    def _prune_old_events(self) -> None:
        """Remove events older than 1 hour to prevent unbounded memory growth."""
        cutoff = int((time.time() - 3600) * 1000)
        self._events = [e for e in self._events if e.timestamp >= cutoff]

    # ================================================================
    # Reconnection
    # ================================================================

    async def _reconnect_backoff(self) -> None:
        """Wait with exponential backoff before reconnecting.

        Binance can block IPs temporarily — wait 5 min if that happens.
        Long quiet periods with no liquidations are normal.
        """
        logger.info(f"Binance liquidation WS: reconnecting in {self._reconnect_delay:.1f}s")
        await asyncio.sleep(self._reconnect_delay)

        self._reconnect_delay = min(
            self._reconnect_delay * settings.RECONNECT_BACKOFF_FACTOR,
            settings.RECONNECT_MAX_DELAY,
        )
