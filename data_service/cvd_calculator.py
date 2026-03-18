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

Warmup: per-window progressive validation.
- 5m VALID after 5 min of continuous trades → CVDState transitions to VALID
- 15m and 1h windows become valid as their spans fill naturally
- This means setups aren't blocked for 1h waiting for the 1h window
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
from data_service.data_integrity import CVDState, CONTRACT_SIZES

logger = setup_logger("data_service")

_OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

# OKX instrument IDs for our pairs
_INST_IDS = {
    "BTC/USDT": "BTC-USDT-SWAP",
    "ETH/USDT": "ETH-USDT-SWAP",
    "SOL/USDT": "SOL-USDT-SWAP",
    "DOGE/USDT": "DOGE-USDT-SWAP",
    "XRP/USDT": "XRP-USDT-SWAP",
    "LINK/USDT": "LINK-USDT-SWAP",
    "AVAX/USDT": "AVAX-USDT-SWAP",
}

# Reverse map
_INST_TO_PAIR = {v: k for k, v in _INST_IDS.items()}

# How often to recalculate CVD snapshots (seconds)
_BATCH_INTERVAL_SEC = 5

# Rolling window durations in milliseconds
_WINDOW_5M_MS = 5 * 60 * 1000
_WINDOW_15M_MS = 15 * 60 * 1000
_WINDOW_1H_MS = 60 * 60 * 1000

# Per-window warmup thresholds (seconds of continuous trade data required)
_WARMUP_5M_SEC = 300     # 5 minutes — enough for 5m window
_WARMUP_15M_SEC = 900    # 15 minutes — enough for 15m window
_WARMUP_1H_SEC = 3600    # 60 minutes — enough for 1h window


@dataclass
class _RawTrade:
    """Internal representation of a single trade from OKX."""
    timestamp: int      # Unix ms
    price: float
    size: float         # In base currency (BTC/ETH), normalized from contracts
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

        # CVD state machine per pair
        self._cvd_state: dict[str, CVDState] = {}
        self._cvd_invalid_reason: dict[str, str] = {}
        # Per-window warmup tracking: which windows have completed warmup
        # Values: set of "5m", "15m", "1h"
        self._warm_windows: dict[str, set[str]] = {}
        # Initialize all configured pairs to WARMING_UP
        for pair in _INST_IDS:
            self._cvd_state[pair] = CVDState.WARMING_UP
            self._cvd_invalid_reason[pair] = "startup"
            self._warm_windows[pair] = set()

        # Stats
        self._trades_received = 0

    # ================================================================
    # Public interface
    # ================================================================

    def get_cvd(self, pair: str) -> CVDSnapshot | None:
        """Get the latest CVD snapshot for a pair.

        Returns None if no trades have been received yet or CVD state is not VALID.
        Snapshot is recalculated every 5 seconds.
        """
        if self._cvd_state.get(pair) != CVDState.VALID:
            return None
        return self._snapshots.get(pair)

    def get_cvd_state(self, pair: str) -> CVDState:
        """Get the current CVD state for a pair."""
        return self._cvd_state.get(pair, CVDState.INVALID)

    def get_cvd_invalid_reason(self, pair: str) -> str:
        """Get the reason CVD is not VALID for a pair."""
        return self._cvd_invalid_reason.get(pair, "unknown")

    def get_warm_windows(self, pair: str) -> set[str]:
        """Get which CVD windows have completed warmup for a pair."""
        return self._warm_windows.get(pair, set()).copy()

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
                # Invalidate CVD on disconnect — trades are lost during downtime
                for pair in self._cvd_state:
                    prev = self._cvd_state[pair]
                    self._cvd_state[pair] = CVDState.INVALID
                    self._cvd_invalid_reason[pair] = "disconnect"
                    if prev != CVDState.INVALID:
                        logger.warning(f"CVD state: {prev.name} → INVALID pair={pair} reason=disconnect")
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

                # Per-window warmup: check each window threshold independently
                if self._cvd_state.get(pair) == CVDState.WARMING_UP:
                    trades = self._trades.get(pair)
                    if trades and len(trades) >= 2:
                        oldest_ms = trades[0].timestamp
                        span_sec = (now_ms - oldest_ms) / 1000
                        warm = self._warm_windows.get(pair, set())

                        # Check each window threshold
                        if "5m" not in warm and span_sec >= _WARMUP_5M_SEC:
                            warm.add("5m")
                            self._warm_windows[pair] = warm
                            # 5m valid → transition to VALID (unblock setups)
                            self._cvd_state[pair] = CVDState.VALID
                            self._cvd_invalid_reason[pair] = ""
                            logger.info(
                                f"CVD state: WARMING_UP → VALID pair={pair} "
                                f"window=5m span={span_sec:.0f}s trades={len(trades)}"
                            )

                        if "15m" not in warm and span_sec >= _WARMUP_15M_SEC:
                            warm.add("15m")
                            self._warm_windows[pair] = warm
                            logger.info(
                                f"CVD window warm: pair={pair} window=15m "
                                f"span={span_sec:.0f}s"
                            )

                        if "1h" not in warm and span_sec >= _WARMUP_1H_SEC:
                            warm.add("1h")
                            self._warm_windows[pair] = warm
                            logger.info(
                                f"CVD fully warm: pair={pair} window=1h "
                                f"span={span_sec:.0f}s (all windows valid)"
                            )

                # Log 15m/1h milestones even after VALID transition
                elif self._cvd_state.get(pair) == CVDState.VALID:
                    trades = self._trades.get(pair)
                    if trades and len(trades) >= 2:
                        oldest_ms = trades[0].timestamp
                        span_sec = (now_ms - oldest_ms) / 1000
                        warm = self._warm_windows.get(pair, set())

                        if "15m" not in warm and span_sec >= _WARMUP_15M_SEC:
                            warm.add("15m")
                            self._warm_windows[pair] = warm
                            logger.info(
                                f"CVD window warm: pair={pair} window=15m "
                                f"span={span_sec:.0f}s"
                            )

                        if "1h" not in warm and span_sec >= _WARMUP_1H_SEC:
                            warm.add("1h")
                            self._warm_windows[pair] = warm
                            logger.info(
                                f"CVD fully warm: pair={pair} window=1h "
                                f"span={span_sec:.0f}s (all windows valid)"
                            )

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
            # Transition to WARMING_UP on reconnect — flush stale trades
            for pair in self._cvd_state:
                if self._cvd_state[pair] == CVDState.INVALID:
                    self._trades[pair].clear()
                    self._snapshots.pop(pair, None)
                    self._cvd_state[pair] = CVDState.WARMING_UP
                    self._cvd_invalid_reason[pair] = "reconnect"
                    self._warm_windows[pair] = set()
                    logger.info(f"CVD state: INVALID → WARMING_UP pair={pair} (trades flushed)")
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

        # Contract size multiplier for this pair
        contract_size = CONTRACT_SIZES.get(pair, 1.0)

        for trade_data in msg.get("data", []):
            try:
                ts = int(trade_data.get("ts", 0))
                price = float(trade_data.get("px", 0))
                size_contracts = float(trade_data.get("sz", 0))
                side = trade_data.get("side", "")

                if price <= 0 or size_contracts <= 0:
                    continue

                if side not in ("buy", "sell"):
                    continue

                # Normalize from contracts to base currency
                size_base = size_contracts * contract_size

                self._trades[pair].append(_RawTrade(
                    timestamp=ts, price=price, size=size_base, side=side
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
