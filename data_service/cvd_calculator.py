"""
CVD (Cumulative Volume Delta) calculator via OKX trades WebSocket.

Subscribes to the trades channel for BTC-USDT-SWAP and ETH-USDT-SWAP.
OKX sends every individual trade with side ("buy"/"sell"), price, size, timestamp.

Instead of processing each trade instantly, we:
1. Accumulate raw trades in memory
2. Every 5 seconds, compute CVD snapshots for 5m, 15m, 1h windows
3. Prune trades older than 1 hour

CVD = sum of (size if buy, -size if sell) over a period.
- CVD rising = aggressive buyers dominate (bullish)
- CVD falling = aggressive sellers dominate (bearish)
- Price up + CVD down = divergence, reversal signal
"""

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import websockets

from config.settings import settings
from shared.logger import setup_logger
from shared.models import CVDSnapshot

logger = setup_logger("data_service")

_OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

# OKX instrument IDs for our pairs
_INST_IDS = {
    "BTC/USDT": "BTC-USDT-SWAP",
    "ETH/USDT": "ETH-USDT-SWAP",
}

# Reverse map
_INST_TO_PAIR = {v: k for k, v in _INST_IDS.items()}

# How often to recalculate CVD snapshots (seconds)
_BATCH_INTERVAL_SEC = 5

# Rolling window durations in milliseconds
_WINDOW_5M_MS = 5 * 60 * 1000
_WINDOW_15M_MS = 15 * 60 * 1000
_WINDOW_1H_MS = 60 * 60 * 1000


@dataclass
class _RawTrade:
    """Internal representation of a single trade from OKX."""
    timestamp: int      # Unix ms
    price: float
    size: float         # In base currency (BTC/ETH)
    side: str           # "buy" or "sell"


class CVDCalculator:
    """Calculates CVD from OKX trade stream with 5-second batching.

    Other services call get_cvd(pair) directly — no pub/sub.
    """

    def __init__(self):
        # Raw trades per pair, sorted by time (newest at end)
        # Pruned to last 1 hour automatically
        self._trades: dict[str, deque[_RawTrade]] = defaultdict(deque)

        # Latest computed CVD snapshot per pair
        self._snapshots: dict[str, CVDSnapshot] = {}

        # Connection state
        self._ws = None
        self._running = False
        self._connected = False
        self._reconnect_delay = settings.RECONNECT_INITIAL_DELAY

        # Stats
        self._trades_received = 0

    # ================================================================
    # Public interface
    # ================================================================

    def get_cvd(self, pair: str) -> CVDSnapshot | None:
        """Get the latest CVD snapshot for a pair.

        Returns None if no trades have been received yet.
        Snapshot is recalculated every 5 seconds.
        """
        return self._snapshots.get(pair)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ================================================================
    # WebSocket lifecycle
    # ================================================================

    async def start(self) -> None:
        """Start WebSocket + batch calculation loop."""
        self._running = True
        # Run WS listener and batch calculator concurrently
        await asyncio.gather(
            self._ws_loop(),
            self._batch_loop(),
        )

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("CVD WebSocket stopped")

    async def _ws_loop(self) -> None:
        """WebSocket connection loop with reconnection."""
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                self._connected = False
                if not self._running:
                    break
                logger.warning(f"CVD WebSocket disconnected. Reason: {e}")
                await self._reconnect_backoff()

    async def _batch_loop(self) -> None:
        """Every 5 seconds, recalculate CVD snapshots and prune old trades."""
        while self._running:
            await asyncio.sleep(_BATCH_INTERVAL_SEC)
            now_ms = int(time.time() * 1000)

            for pair in list(self._trades.keys()):
                self._prune_old_trades(pair, now_ms)
                self._compute_snapshot(pair, now_ms)

    # ================================================================
    # Internal: connect, subscribe, parse trades
    # ================================================================

    async def _connect_and_listen(self) -> None:
        logger.info(f"CVD WebSocket connecting to {_OKX_WS_URL}")

        async with websockets.connect(
            _OKX_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = settings.RECONNECT_INITIAL_DELAY

            logger.info("CVD WebSocket connected")
            await self._subscribe(ws)

            async for raw_msg in ws:
                # OKX sends text "pong" for keepalive
                if raw_msg == "pong":
                    continue

                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                # Handle subscription confirmations
                if "event" in msg:
                    event = msg.get("event", "")
                    if event == "subscribe":
                        arg = msg.get("arg", {})
                        logger.info(f"CVD subscribed: {arg.get('channel')} "
                                    f"instId={arg.get('instId')}")
                    continue

                # Handle trade data
                if "data" in msg and "arg" in msg:
                    self._handle_trades(msg)

    async def _subscribe(self, ws) -> None:
        args = [
            {"channel": "trades", "instId": inst_id}
            for inst_id in _INST_IDS.values()
        ]
        sub_msg = {"op": "subscribe", "args": args}
        await ws.send(json.dumps(sub_msg))
        logger.info(f"CVD subscribing to trades for {len(_INST_IDS)} instruments")

    def _handle_trades(self, msg: dict) -> None:
        """Parse OKX trade messages.

        OKX trades format:
        {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "tradeId": "12345",
                    "px": "65000.5",
                    "sz": "0.01",
                    "side": "buy",
                    "ts": "1709500000000"
                }
            ]
        }

        Side: "buy" = taker bought (bullish), "sell" = taker sold (bearish).
        """
        arg = msg.get("arg", {})
        inst_id = arg.get("instId", "")
        pair = _INST_TO_PAIR.get(inst_id)
        if not pair:
            return

        for trade_data in msg.get("data", []):
            try:
                ts = int(trade_data.get("ts", 0))
                price = float(trade_data.get("px", 0))
                size = float(trade_data.get("sz", 0))
                side = trade_data.get("side", "")

                if price <= 0 or size <= 0:
                    continue

                if side not in ("buy", "sell"):
                    continue

                self._trades[pair].append(_RawTrade(
                    timestamp=ts, price=price, size=size, side=side
                ))
                self._trades_received += 1

            except (ValueError, TypeError):
                continue

    # ================================================================
    # CVD calculation
    # ================================================================

    def _compute_snapshot(self, pair: str, now_ms: int) -> None:
        """Calculate CVD for 5m, 15m, 1h windows from raw trades."""
        trades = self._trades.get(pair)
        if not trades:
            return

        cutoff_5m = now_ms - _WINDOW_5M_MS
        cutoff_15m = now_ms - _WINDOW_15M_MS
        cutoff_1h = now_ms - _WINDOW_1H_MS

        cvd_5m = 0.0
        cvd_15m = 0.0
        cvd_1h = 0.0
        buy_vol = 0.0
        sell_vol = 0.0

        for trade in trades:
            delta = trade.size if trade.side == "buy" else -trade.size

            if trade.timestamp >= cutoff_1h:
                cvd_1h += delta
                if trade.side == "buy":
                    buy_vol += trade.size
                else:
                    sell_vol += trade.size

            if trade.timestamp >= cutoff_15m:
                cvd_15m += delta

            if trade.timestamp >= cutoff_5m:
                cvd_5m += delta

        self._snapshots[pair] = CVDSnapshot(
            timestamp=now_ms,
            pair=pair,
            cvd_5m=cvd_5m,
            cvd_15m=cvd_15m,
            cvd_1h=cvd_1h,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
        )

    def _prune_old_trades(self, pair: str, now_ms: int) -> None:
        """Remove trades older than 1 hour to bound memory usage."""
        cutoff = now_ms - _WINDOW_1H_MS
        trades = self._trades.get(pair)
        if not trades:
            return
        while trades and trades[0].timestamp < cutoff:
            trades.popleft()

    # ================================================================
    # Reconnection
    # ================================================================

    async def _reconnect_backoff(self) -> None:
        logger.info(f"CVD WebSocket: reconnecting in {self._reconnect_delay:.1f}s")
        await asyncio.sleep(self._reconnect_delay)
        self._reconnect_delay = min(
            self._reconnect_delay * settings.RECONNECT_BACKOFF_FACTOR,
            settings.RECONNECT_MAX_DELAY,
        )
