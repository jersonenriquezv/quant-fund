"""Dual Thrust shadow tracker — order-free theoretical flip position.

Phase 2 of docs/plans/dual-thrust-phase1b-shadow-wiring.md. On each confirmed
ETH/USDT 4h candle (timing trigger only) the bot fetches the trailing OKX REST
``4H`` window — the authoritative source Phase 0/1a validated against, NOT the
bot's own candle store — replays the validated Dual Thrust brain
(``strategy_service.engines.dual_thrust``) plus the harness fill model, and
records a theoretical flip position. Places NO orders, touches no
``risk_service``/``execution_service`` order path.

WHY re-fetch + full replay each close (vs incremental state): the harness fill
model is deterministic over the whole window, so re-running it on fresh REST
bars guarantees trade-for-trade parity with the validated harness by
construction and is robust to restarts (no drift, no persisted position to
corrupt).

The fill loop is a VERBATIM port of ``okx_revalidation.backtest`` (funding=None
path), the same loop ``scripts/dual_thrust_parity.py`` proves the engine against.
Copied here so the bot does not depend on the ``~/jesse-research`` harness. If
you change this math, the parity check (``scripts/dual_thrust_parity.py``) must
still pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from shared.logger import setup_logger
from shared.models import Candle
from strategy_service.engines import dual_thrust as DT

logger = setup_logger("dual_thrust_shadow")

# --- Harness constants, VERBATIM from ~/jesse-research/project/okx_revalidation.py
DT_RISK_PCT = 2.0          # risk_to_qty(balance, 2, ...)
DT_START_BALANCE = 10_000.0
DT_FEE_RATE = 0.0005       # 0.05% per side


@dataclass(frozen=True)
class ShadowTrade:
    """One completed theoretical trade (entry → exit)."""
    entry_ts: int
    exit_ts: int
    side: int          # +1 long, -1 short
    entry: float
    exit: float
    qty: float
    pnl_net: float
    reason: str        # "flip" | "sl"


@dataclass(frozen=True)
class ShadowState:
    """Result of a full replay over the fetched window."""
    last_ts: int                       # timestamp of the last closed bar
    signal: int                        # raw DT signal on the last bar (+1/-1/0)
    position_side: int                 # current theoretical position (0 = flat)
    position_entry: Optional[float]
    position_stop: Optional[float]
    balance: float                     # theoretical balance after closed trades
    trades: tuple[ShadowTrade, ...]    # all closed trades over the window


def simulate_fills(candles: Sequence[Candle], hp: dict) -> ShadowState:
    """Replay the Dual Thrust brain + harness fill model over ``candles``.

    VERBATIM port of the harness fill loop: entries/flips fill at the NEXT bar's
    open (deferred), the stop fills intrabar at the stop price. ``candles`` must
    be confirmed, chronological, single-pair, single-TF (4h).
    """
    ts = [c.timestamp for c in candles]
    o = [c.open for c in candles]
    h = [c.high for c in candles]
    low = [c.low for c in candles]
    c_ = [c.close for c in candles]

    bars = DT.replay_signals(ts, o, h, low, c_, hp)
    bar_by_ts = {b.timestamp: b for b in bars}

    balance = DT_START_BALANCE
    pos: Optional[dict] = None
    trades: list[ShadowTrade] = []
    pend: Optional[tuple] = None

    def close_pos(exit_price: float, reason: str, bar_ts: int) -> None:
        nonlocal balance, pos
        assert pos is not None
        pnl = pos["qty"] * (exit_price - pos["entry"]) * pos["side"]
        fees = (pos["entry"] + exit_price) * pos["qty"] * DT_FEE_RATE
        net = pnl - fees
        balance += net
        trades.append(ShadowTrade(
            entry_ts=pos["entry_ts"], exit_ts=bar_ts, side=pos["side"],
            entry=pos["entry"], exit=exit_price, qty=pos["qty"],
            pnl_net=net, reason=reason))
        pos = None

    def open_pos(side: int, price: float, a: float, bar_ts: int) -> None:
        nonlocal pos
        stop = price - side * a * hp["stop_loss_atr_rate"]
        qty = (balance * DT_RISK_PCT / 100.0) / abs(price - stop)
        pos = {"side": side, "entry": price, "stop": stop, "qty": qty,
               "entry_ts": bar_ts}

    upL, dnL = hp["up_length"], hp["down_length"]
    warm = max(upL, dnL, DT.ATR_PERIOD)
    for i in range(warm, len(c_)):
        # (a) execute pending action at this bar's open
        if pend is not None:
            if pend[0] == "close" and pos is not None:
                close_pos(o[i], "flip", ts[i])
            elif pend[0] == "enter" and pos is None:
                open_pos(pend[1], o[i], pend[2], ts[i])
            pend = None

        # (b) intrabar stop
        if pos is not None:
            if pos["side"] == 1 and low[i] <= pos["stop"]:
                close_pos(pos["stop"], "sl", ts[i])
            elif pos["side"] == -1 and h[i] >= pos["stop"]:
                close_pos(pos["stop"], "sl", ts[i])

        # (c) signal from the engine
        bar = bar_by_ts.get(int(ts[i]))
        if bar is None or bar.anchor_open is None or math.isnan(bar.atr):
            pend = None
            continue
        sig = bar.sig
        if pos is not None:
            pend = ("close",) if (sig != 0 and sig != pos["side"]) else None
        else:
            pend = ("enter", sig, bar.atr) if sig != 0 else None

    last = bars[-1] if bars else None
    return ShadowState(
        last_ts=int(ts[-1]) if ts else 0,
        signal=last.sig if last else 0,
        position_side=pos["side"] if pos else 0,
        position_entry=pos["entry"] if pos else None,
        position_stop=pos["stop"] if pos else None,
        balance=balance,
        trades=tuple(trades),
    )


class DualThrustShadowTracker:
    """Order-free shadow evaluator wired into the confirmed-candle pipeline.

    ``candle_fetcher()`` must return the trailing OKX REST 4h closed candles
    (oldest-first). In the live bot this is ``exchange_client.backfill_candles``
    (forming bar already dropped → authoritative). Injected for testability.
    """

    PAIR = "ETH/USDT"
    TF = "4h"

    def __init__(self, candle_fetcher: Callable[[], Sequence[Candle]],
                 fetch_count: int = 500):
        self._fetch = candle_fetcher
        self._fetch_count = fetch_count
        self._last_processed_ts: int = 0
        self._last_trade_count: int = 0

    def on_candle(self, candle: Candle) -> Optional[ShadowState]:
        """Evaluate on a confirmed ETH 4h candle. Returns the replay state.

        Never raises into the pipeline — caller still wraps in try/except, but
        this method itself swallows fetch/replay errors and returns None.
        """
        if candle.pair != self.PAIR or candle.timeframe != self.TF:
            return None
        try:
            candles = list(self._fetch())
            hp = DT.DUAL_THRUST_PARAMS[self.TF]
            if len(candles) < max(hp["up_length"], hp["down_length"], DT.ATR_PERIOD) + 1:
                logger.warning(f"Dual Thrust shadow: insufficient candles "
                               f"({len(candles)}) — skipping")
                return None
            state = simulate_fills(candles, hp)
        except Exception as e:  # never break the pipeline
            logger.error(f"Dual Thrust shadow replay failed: {e}")
            return None

        # Dedup: only act/log once per closed bar.
        if state.last_ts == self._last_processed_ts:
            return state
        self._last_processed_ts = state.last_ts

        side_name = {1: "LONG", -1: "SHORT", 0: "FLAT"}
        new_trades = state.trades[self._last_trade_count:]
        self._last_trade_count = len(state.trades)

        for tr in new_trades:
            logger.info(
                f"DT_SHADOW_TRADE pair={self.PAIR} side={side_name[tr.side]} "
                f"entry={tr.entry:.2f} exit={tr.exit:.2f} reason={tr.reason} "
                f"pnl_net={tr.pnl_net:.2f} entry_ts={tr.entry_ts} exit_ts={tr.exit_ts}")

        logger.info(
            f"DT_SHADOW_STATE ts={state.last_ts} signal={side_name[state.signal]} "
            f"position={side_name[state.position_side]} "
            f"entry={state.position_entry} stop={state.position_stop} "
            f"balance={state.balance:.2f} trades={len(state.trades)}")
        return state
