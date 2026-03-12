"""
OKX WebSocket client for real-time candle data.

Subscribes to candle channels for BTC-USDT-SWAP and ETH-USDT-SWAP
across all timeframes (5m, 15m, 1h, 4h).

OKX sends candle updates with a "confirm" field:
- confirm="0": candle is still forming (in-progress update)
- confirm="1": candle is closed/confirmed — only process these

Reconnection: exponential backoff per data-engineer spec.
"""

import asyncio
import json
import time
from collections import defaultdict

import websockets

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle

logger = setup_logger("data_service")

# OKX business WebSocket URL (candle channels live here, NOT on /public)
_OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/business"

# Instrument IDs for OKX perpetuals
_INST_IDS = {
    "BTC/USDT": "BTC-USDT-SWAP",
    "ETH/USDT": "ETH-USDT-SWAP",
}

# Reverse map: instId back to our pair format
_INST_TO_PAIR = {v: k for k, v in _INST_IDS.items()}

# OKX candle channel naming: "candle" + timeframe
# e.g., "candle5m", "candle15m", "candle1H", "candle4H"
_TIMEFRAME_TO_CHANNEL = {
    "5m": "candle5m",
    "15m": "candle15m",
    "1h": "candle1H",
    "4h": "candle4H",
    "1d": "candle1D",
}

# Reverse map
_CHANNEL_TO_TIMEFRAME = {v: k for k, v in _TIMEFRAME_TO_CHANNEL.items()}

# How many candles to keep in memory per pair/timeframe
_MAX_CANDLES_IN_MEMORY = 600


class OKXWebSocketFeed:
    """Persistent WebSocket connection to OKX for real-time candle data.

    Stores confirmed candles in memory. Other services read via
    get_latest_candle() and get_candles() — direct function calls.
    """

    def __init__(self, on_candle_confirmed=None, metrics_callback=None):
        """
        Args:
            on_candle_confirmed: Optional async callback called when a new
                confirmed candle arrives. Signature: async fn(candle: Candle).
                This is how main.py triggers the pipeline.
            metrics_callback: Optional fn(name, value, pair, labels) for operational metrics.
        """
        # Storage: {("BTC/USDT", "5m"): [Candle, Candle, ...]}
        self._candles: dict[tuple[str, str], list[Candle]] = defaultdict(list)

        # Callback for pipeline trigger
        self._on_candle_confirmed = on_candle_confirmed

        # Metrics callback for Grafana
        self._metrics_cb = metrics_callback

        # Connection state
        self._ws = None
        self._running = False
        self._connected = False

        # Per-pair locks to serialize pipeline execution
        self._pipeline_locks: dict[str, asyncio.Lock] = {}

        # Dedup: track last confirmed candle timestamp per (pair, timeframe)
        # Prevents duplicate pipeline runs if OKX sends the same candle twice
        self._last_confirmed_ts: dict[tuple[str, str], int] = {}

        # Reconnection backoff state
        self._reconnect_delay = settings.RECONNECT_INITIAL_DELAY
        self._last_message_time = 0.0

    # ================================================================
    # Public interface — called by other services via direct import
    # ================================================================

    def get_latest_candle(self, pair: str, timeframe: str) -> Candle | None:
        """Get the most recent confirmed candle for a pair/timeframe."""
        key = (pair, timeframe)
        candles = self._candles.get(key)
        if not candles:
            return None
        return candles[-1]

    def get_candles(self, pair: str, timeframe: str,
                    count: int = 100) -> list[Candle]:
        """Get last N confirmed candles for a pair/timeframe.
        Returns oldest-first ordering.
        """
        key = (pair, timeframe)
        candles = self._candles.get(key, [])
        return candles[-count:]

    def store_candles(self, candles: list[Candle]) -> None:
        """Store backfilled candles. Called by DataService after backfill."""
        for candle in candles:
            key = (candle.pair, candle.timeframe)
            self._candles[key].append(candle)

        # Deduplicate by timestamp and trim
        for key in self._candles:
            seen = set()
            unique = []
            for c in self._candles[key]:
                if c.timestamp not in seen:
                    seen.add(c.timestamp)
                    unique.append(c)
            unique.sort(key=lambda x: x.timestamp)
            self._candles[key] = unique[-_MAX_CANDLES_IN_MEMORY:]

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ================================================================
    # WebSocket lifecycle
    # ================================================================

    async def start(self) -> None:
        """Start the WebSocket connection with automatic reconnection."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                self._connected = False
                if not self._running:
                    break
                logger.warning(f"OKX WebSocket disconnected. Reason: {e}")
                if self._metrics_cb:
                    self._metrics_cb("ws_reconnection", 1.0, None, {"feed": "candles"})
                await self._reconnect_backoff()

    async def stop(self) -> None:
        """Gracefully stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("OKX WebSocket stopped gracefully")

    # ================================================================
    # Internal: connect, subscribe, listen
    # ================================================================

    async def _connect_and_listen(self) -> None:
        """Connect to OKX WebSocket and listen for messages."""
        logger.info(f"OKX WebSocket connecting to {_OKX_WS_URL}")

        async with websockets.connect(
            _OKX_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._reconnect_delay = settings.RECONNECT_INITIAL_DELAY
            self._last_message_time = time.time()

            logger.info("OKX WebSocket connected")
            await self._subscribe_all(ws)

            # Listen loop
            async for raw_msg in ws:
                self._last_message_time = time.time()

                # OKX sends text "pong" for keepalive
                if raw_msg == "pong":
                    continue

                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    logger.warning(f"OKX WS: non-JSON message: {raw_msg[:200]}")
                    continue

                # Handle subscription confirmations
                if "event" in msg:
                    self._handle_event(msg)
                    continue

                # Handle candle data
                if "data" in msg and "arg" in msg:
                    self._handle_candle_data(msg)

    async def _subscribe_all(self, ws) -> None:
        """Subscribe to all candle channels for all pairs."""
        args = []
        for inst_id in _INST_IDS.values():
            for channel in _TIMEFRAME_TO_CHANNEL.values():
                args.append({
                    "channel": channel,
                    "instId": inst_id,
                })

        sub_msg = {"op": "subscribe", "args": args}
        await ws.send(json.dumps(sub_msg))

        logger.info(f"OKX WebSocket: subscribing to {len(args)} channels "
                    f"({len(_INST_IDS)} pairs x {len(_TIMEFRAME_TO_CHANNEL)} timeframes)")

    def _handle_event(self, msg: dict) -> None:
        """Handle OKX event messages (subscribe confirmations, errors)."""
        event = msg.get("event", "")
        if event == "subscribe":
            arg = msg.get("arg", {})
            logger.info(f"OKX WS: subscribed to {arg.get('channel')} "
                        f"instId={arg.get('instId')}")
        elif event == "error":
            logger.error(f"OKX WS error: code={msg.get('code')} msg={msg.get('msg')}")

    def _handle_candle_data(self, msg: dict) -> None:
        """Parse OKX candle data from WebSocket message.

        OKX candle message format:
        {
            "arg": {
                "channel": "candle5m",
                "instId": "BTC-USDT-SWAP"
            },
            "data": [
                [
                    "1709500000000",   // timestamp (ms)
                    "65000.0",         // open
                    "65200.0",         // high
                    "64900.0",         // low
                    "65100.0",         // close
                    "10.5",            // volume (contracts)
                    "682050",          // volume (currency)
                    "682050",          // volume (quote)
                    "1"                // confirm: "1" = closed, "0" = forming
                ]
            ]
        }

        We ONLY process candles with confirm="1" (closed candles).
        """
        arg = msg.get("arg", {})
        channel = arg.get("channel", "")
        inst_id = arg.get("instId", "")

        timeframe = _CHANNEL_TO_TIMEFRAME.get(channel)
        pair = _INST_TO_PAIR.get(inst_id)

        if not timeframe or not pair:
            return

        for candle_data in msg.get("data", []):
            if len(candle_data) < 9:
                continue

            # Only process confirmed candles
            confirm = candle_data[8]
            if confirm != "1":
                continue

            try:
                ts = int(candle_data[0])
                o = float(candle_data[1])
                h = float(candle_data[2])
                l = float(candle_data[3])
                c = float(candle_data[4])
                # candle_data[5] = vol in contracts, [6] = volCcy in base currency
                # Use base currency (index 6) to match ccxt REST backfill units
                vol = float(candle_data[6]) if candle_data[6] else float(candle_data[5])
                vol_quote = float(candle_data[7]) if candle_data[7] else vol * c
            except (ValueError, IndexError) as e:
                logger.error(f"OKX candle parse error: pair={pair} tf={timeframe} "
                             f"data={candle_data} error={e}")
                continue

            # Validate
            if not self._validate_candle(pair, timeframe, ts, o, h, l, c, vol):
                continue

            candle = Candle(
                timestamp=ts,
                open=o,
                high=h,
                low=l,
                close=c,
                volume=vol,
                volume_quote=vol_quote,
                pair=pair,
                timeframe=timeframe,
                confirmed=True,
            )

            # Deduplicate: skip if we already processed this exact timestamp
            key = (pair, timeframe)
            if self._last_confirmed_ts.get(key) == ts:
                logger.debug(f"Duplicate candle skipped: pair={pair} tf={timeframe} ts={ts}")
                continue
            self._last_confirmed_ts[key] = ts

            # Store in memory
            self._candles[key].append(candle)

            # Trim to max size
            if len(self._candles[key]) > _MAX_CANDLES_IN_MEMORY:
                self._candles[key] = self._candles[key][-_MAX_CANDLES_IN_MEMORY:]

            logger.info(f"Candle confirmed: pair={pair} tf={timeframe} "
                        f"close={c} vol={vol:.4f} ts={ts}")

            # Trigger pipeline callback (serialized per pair)
            if self._on_candle_confirmed:
                task = asyncio.get_running_loop().create_task(
                    self._run_pipeline_serialized(candle)
                )
                task.add_done_callback(self._pipeline_task_done)

    # ================================================================
    # Pipeline serialization
    # ================================================================

    async def _run_pipeline_serialized(self, candle: Candle) -> None:
        """Run pipeline callback while holding the per-pair lock."""
        if candle.pair not in self._pipeline_locks:
            self._pipeline_locks[candle.pair] = asyncio.Lock()

        async with self._pipeline_locks[candle.pair]:
            await self._on_candle_confirmed(candle)

    @staticmethod
    def _pipeline_task_done(task: asyncio.Task) -> None:
        """Log exceptions from pipeline tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Pipeline task failed: {exc}", exc_info=exc)

    # ================================================================
    # Validation
    # ================================================================

    @staticmethod
    def _validate_candle(pair: str, timeframe: str, ts: int,
                         o: float, h: float, l: float, c: float,
                         vol: float) -> bool:
        """Validate candle per data-engineer rules."""
        now_ms = int(time.time() * 1000)

        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            logger.error(f"WS candle price <= 0 discarded: pair={pair} tf={timeframe} "
                         f"OHLC=[{o},{h},{l},{c}]")
            return False

        if vol == 0:
            logger.warning(f"WS candle zero volume discarded: pair={pair} tf={timeframe} ts={ts}")
            return False

        if ts > now_ms + 60_000:
            logger.warning(f"WS candle future timestamp discarded: pair={pair} tf={timeframe} "
                           f"ts={ts} diff={ts - now_ms}ms")
            return False

        return True

    # ================================================================
    # Reconnection — exponential backoff
    # ================================================================

    async def _reconnect_backoff(self) -> None:
        """Wait with exponential backoff before reconnecting.

        Sequence: 1s -> 2s -> 4s -> 8s -> ... -> 60s max.
        """
        logger.info(f"OKX WebSocket: reconnecting in {self._reconnect_delay:.1f}s")
        await asyncio.sleep(self._reconnect_delay)

        self._reconnect_delay = min(
            self._reconnect_delay * settings.RECONNECT_BACKOFF_FACTOR,
            settings.RECONNECT_MAX_DELAY,
        )
