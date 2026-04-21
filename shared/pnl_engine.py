"""
Unified P&L + TP/SL/BE engine.

Single source of truth for:
- Breakeven logic (TP1 touched → SL moves to entry)
- TP / SL / BE resolution from candle sequences
- P&L computation with per-side fees

Used by shadow_monitor, backtest simulator, and execution monitor so all
three engines agree on outcomes. Pure — no DB, no I/O, no side effects.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Outcome(str, Enum):
    TP = "tp"
    SL = "sl"
    BREAKEVEN = "breakeven"
    TIMEOUT = "timeout"
    NO_FILL = "no_fill"
    PENDING = "pending"


@dataclass
class Position:
    direction: str                # "long" | "short"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    position_size: float          # base currency units
    # Mutable state (advanced by step())
    filled: bool = False
    tp1_touched: bool = False
    outcome: Outcome = Outcome.PENDING
    exit_price: float = 0.0
    # BE confirmation — number of full candle CLOSES above TP1 before
    # SL is moved to entry. 0 = legacy behavior (touch is enough).
    # Raising to 1+ prevents wick-through false triggers.
    be_confirm_closes: int = 0
    _tp1_touches_observed: int = 0


@dataclass
class CandleSlice:
    """Minimal candle interface the engine needs. Use real Candle objects too."""
    high: float
    low: float
    close: float
    timestamp: int = 0


@dataclass
class PnL:
    gross_usd: float
    fee_usd: float
    net_usd: float
    pct: float   # net return vs entry notional (fee-inclusive)


def compute_pnl(
    entry: float, exit_price: float, size: float, direction: str,
    fee_rate: float,
) -> PnL:
    """P&L net of two-sided taker fees.

    entry, exit_price: prices in quote currency.
    size: position in base currency.
    direction: "long" | "short".
    fee_rate: per-side rate (e.g. 0.0005 = 5 bps).
    """
    if size <= 0 or entry <= 0:
        return PnL(0.0, 0.0, 0.0, 0.0)

    if direction == "long":
        gross = (exit_price - entry) * size
    elif direction == "short":
        gross = (entry - exit_price) * size
    else:
        raise ValueError(f"direction must be long|short, got {direction!r}")

    fee = (entry * size + exit_price * size) * fee_rate
    net = gross - fee
    pct = net / (entry * size)
    return PnL(gross_usd=gross, fee_usd=fee, net_usd=net, pct=pct)


def _touched(candle: CandleSlice, price: float) -> bool:
    return candle.low <= price <= candle.high


def _closed_through(candle: CandleSlice, price: float, direction: str) -> bool:
    """True if candle CLOSED beyond the TP1 price (not just wick)."""
    if direction == "long":
        return candle.close >= price
    return candle.close <= price


def step(position: Position, candle: CandleSlice) -> Outcome:
    """Advance one candle through a filled position. Returns outcome.

    Outcome.PENDING = no resolution yet.
    Outcome.TP / SL / BREAKEVEN = terminal.

    Call only on filled positions. Raises if not filled.
    """
    if not position.filled:
        raise ValueError("step() requires filled position. Call fill() first.")
    if position.outcome != Outcome.PENDING:
        return position.outcome

    # Track TP1 touch + BE activation.
    # tp1_just_activated guards against same-candle spurious BE: on the
    # candle that first moves SL to entry, the candle range by construction
    # includes entry. Skip SL check that candle.
    #
    # be_confirm_closes=0: legacy — any touch (wick OK) arms BE immediately.
    # be_confirm_closes=N≥1: need N candle CLOSES through TP1 before arming.
    # Wicks that don't close-through do NOT count toward confirmation.
    tp1_just_activated = False
    if not position.tp1_touched and _touched(candle, position.tp1_price):
        if position.be_confirm_closes == 0:
            position.tp1_touched = True
            position.sl_price = position.entry_price
            tp1_just_activated = True
        elif _closed_through(candle, position.tp1_price, position.direction):
            position._tp1_touches_observed += 1
            if position._tp1_touches_observed >= position.be_confirm_closes:
                position.tp1_touched = True
                position.sl_price = position.entry_price
                tp1_just_activated = True

    hit_tp = _touched(candle, position.tp2_price)
    hit_sl = False if tp1_just_activated else _touched(candle, position.sl_price)

    if hit_tp and hit_sl:
        # Both in same candle — conservative. If BE armed, call BE. Else SL.
        position.exit_price = position.entry_price if position.tp1_touched else position.sl_price
        position.outcome = Outcome.BREAKEVEN if position.tp1_touched else Outcome.SL
        return position.outcome
    if hit_tp:
        position.exit_price = position.tp2_price
        position.outcome = Outcome.TP
        return position.outcome
    if hit_sl:
        # BE if tp1 was armed (SL now = entry); otherwise real SL loss.
        position.exit_price = position.sl_price
        position.outcome = Outcome.BREAKEVEN if position.tp1_touched else Outcome.SL
        return position.outcome

    return Outcome.PENDING


def try_fill(position: Position, candle: CandleSlice) -> bool:
    """Attempt to fill entry. Mutates position.filled. Returns True if filled."""
    if position.filled:
        return True
    if _touched(candle, position.entry_price):
        position.filled = True
        return True
    return False


def simulate(
    position: Position, candles: Iterable[CandleSlice],
    fee_rate: float,
) -> tuple[Outcome, PnL]:
    """Replay a candle sequence. Returns (outcome, pnl).

    - If entry never touched → (NO_FILL, zero PnL).
    - If filled but no TP/SL hit → (TIMEOUT, PnL at last close).
    """
    last_close = 0.0
    for candle in candles:
        last_close = candle.close
        if not position.filled:
            if not try_fill(position, candle):
                continue
            # Same-candle TP/SL check after fill
        outcome = step(position, candle)
        if outcome != Outcome.PENDING:
            return outcome, compute_pnl(
                position.entry_price, position.exit_price,
                position.position_size, position.direction, fee_rate,
            )

    if not position.filled:
        return Outcome.NO_FILL, PnL(0.0, 0.0, 0.0, 0.0)

    # Filled, no resolution — timeout at last close
    position.exit_price = last_close
    position.outcome = Outcome.TIMEOUT
    return Outcome.TIMEOUT, compute_pnl(
        position.entry_price, last_close,
        position.position_size, position.direction, fee_rate,
    )
