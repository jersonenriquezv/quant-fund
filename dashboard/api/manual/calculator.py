"""Position sizing & R:R calculator for manual trades.

Supports two margin types:
- linear (USDT-margined): position_size in base asset, PnL in USDT
- inverse (coin-margined): position_size in USD contracts, PnL in coin → USD

No imports from risk_service — self-contained math.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TPLevel:
    price: float
    rr_ratio: float
    close_pct: float  # e.g. 50.0
    size_to_close: float
    potential_profit_usd: float
    after_action: str | None = None  # e.g. "Move SL to breakeven"


@dataclass
class Advice:
    level: Literal["info", "warn", "danger"]
    message: str
    action: str | None = None  # actionable suggestion


@dataclass
class CalculatorResult:
    # Input echo
    pair: str
    direction: str
    margin_type: str  # "linear" or "inverse"
    balance: float
    risk_percent: float
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    leverage: int

    # Calculations
    risk_usd: float
    sl_distance: float
    sl_distance_pct: float
    position_size: float  # base asset (linear) or USD contracts (inverse)
    position_size_label: str  # e.g. "SOL" or "contracts"
    position_value_usd: float
    margin_required: float
    margin_currency: str  # "USDT" or base coin (e.g. "BTC")
    margin_pct_of_balance: float

    # TP plan
    tp_plan: list[TPLevel]
    total_potential_profit: float
    total_potential_loss: float

    # Suggestions & warnings
    suggested_tp1: float
    suggested_tp2: float
    warnings: list[str] = field(default_factory=list)
    advice: list[Advice] = field(default_factory=list)


def calculate(
    pair: str,
    direction: str,
    balance: float,
    risk_percent: float,
    entry: float,
    stop_loss: float,
    take_profit_1: float | None = None,
    take_profit_2: float | None = None,
    leverage: int = 7,
    margin_type: str = "linear",
) -> CalculatorResult:
    """Calculate position sizing, R:R, and TP plan.

    margin_type:
        "linear" — USDT-margined. Balance in USDT. Position in base asset.
        "inverse" — Coin-margined. Balance in USDT. Position in USD contracts.
                     PnL = contracts × (close - entry) / entry for longs.

    TP strategy: if TP2 provided → 50/50 split (close 50% at TP1, move SL to BE, 50% at TP2).
    If only TP1 → close 100% at TP1.
    """
    if balance <= 0:
        raise ValueError("Balance must be positive")
    if risk_percent <= 0:
        raise ValueError("Risk percent must be positive")
    if entry <= 0 or stop_loss <= 0:
        raise ValueError("Prices must be positive")
    if direction not in ("long", "short"):
        raise ValueError("Direction must be 'long' or 'short'")
    if margin_type not in ("linear", "inverse"):
        raise ValueError("margin_type must be 'linear' or 'inverse'")

    # Validate SL direction
    if direction == "long" and stop_loss >= entry:
        raise ValueError("Long trade: stop loss must be below entry")
    if direction == "short" and stop_loss <= entry:
        raise ValueError("Short trade: stop loss must be above entry")

    # Common
    risk_usd = balance * (risk_percent / 100)
    sl_distance = abs(entry - stop_loss)
    sl_distance_pct = (sl_distance / entry) * 100

    base_coin = pair.split("/")[0]  # e.g. "BTC" from "BTC/USD"

    if margin_type == "linear":
        position_size, position_value_usd, margin_required, margin_currency, size_label = \
            _calc_linear(risk_usd, sl_distance, entry, leverage, base_coin)
    else:
        position_size, position_value_usd, margin_required, margin_currency, size_label = \
            _calc_inverse(risk_usd, sl_distance, sl_distance_pct, entry, leverage, base_coin)

    margin_pct = (margin_required / balance) * 100 if margin_type == "linear" else 0.0
    # For inverse, margin is in coin — compare USD equivalent to balance
    if margin_type == "inverse":
        margin_usd_equiv = margin_required * entry  # coin × price = USD
        margin_pct = (margin_usd_equiv / balance) * 100

    # Auto-suggest TPs
    if direction == "long":
        suggested_tp1 = entry + sl_distance
        suggested_tp2 = entry + 2 * sl_distance
    else:
        suggested_tp1 = entry - sl_distance
        suggested_tp2 = entry - 2 * sl_distance

    tp1 = take_profit_1 if take_profit_1 is not None else suggested_tp1
    tp2 = take_profit_2  # TP2 only when explicitly provided

    # Validate TP direction
    if direction == "long":
        if tp1 <= entry:
            raise ValueError("Long trade: TP1 must be above entry")
        if tp2 is not None and tp2 <= entry:
            raise ValueError("Long trade: TP2 must be above entry")
    else:
        if tp1 >= entry:
            raise ValueError("Short trade: TP1 must be below entry")
        if tp2 is not None and tp2 >= entry:
            raise ValueError("Short trade: TP2 must be below entry")

    # TP plan: 100% at TP1 if no TP2, else 50/50 split
    has_tp2 = tp2 is not None
    tp1_size = position_size / 2 if has_tp2 else position_size
    tp1_close_pct = 50.0 if has_tp2 else 100.0

    tp1_reward = abs(tp1 - entry)
    tp1_rr = tp1_reward / sl_distance
    tp1_profit = _pnl(margin_type, direction, entry, tp1, tp1_size)

    tp_plan = [
        TPLevel(
            price=tp1,
            rr_ratio=round(tp1_rr, 2),
            close_pct=tp1_close_pct,
            size_to_close=tp1_size,
            potential_profit_usd=round(tp1_profit, 2),
            after_action=f"Move SL to breakeven at {entry}" if has_tp2 else None,
        ),
    ]

    tp2_profit = 0.0
    if has_tp2:
        tp2_size = position_size / 2
        tp2_reward = abs(tp2 - entry)
        tp2_rr = tp2_reward / sl_distance
        tp2_profit = _pnl(margin_type, direction, entry, tp2, tp2_size)
        tp_plan.append(
            TPLevel(
                price=tp2,
                rr_ratio=round(tp2_rr, 2),
                close_pct=50.0,
                size_to_close=tp2_size,
                potential_profit_usd=round(tp2_profit, 2),
            ),
        )

    total_profit = tp1_profit + tp2_profit
    total_loss = risk_usd

    # Warnings (legacy) + actionable advice
    warnings: list[str] = []
    advice: list[Advice] = []

    tp2_rr_val = abs(tp2 - entry) / sl_distance if has_tp2 else 0.0

    # ── SL distance checks ──
    if sl_distance_pct < 0.5:
        ideal_sl_dist = entry * 0.015  # 1.5%
        ideal_sl = entry - ideal_sl_dist if direction == "long" else entry + ideal_sl_dist
        ideal_risk_pct = risk_percent * (0.5 / sl_distance_pct)
        warnings.append("SL too tight — likely noise")
        advice.append(Advice(
            level="danger",
            message=f"SL at {sl_distance_pct:.2f}% — will get stopped by noise",
            action=f"Widen SL to ~${ideal_sl:.2f} (1.5%) or reduce risk to {min(ideal_risk_pct, risk_percent):.1f}%",
        ))
    elif sl_distance_pct < 1.0:
        warnings.append("SL too tight — likely noise")
        advice.append(Advice(
            level="warn",
            message=f"SL at {sl_distance_pct:.2f}% — tight for most setups",
            action=f"Consider widening SL or reducing risk% to {risk_percent * 0.5:.1f}% to keep position smaller",
        ))
    elif sl_distance_pct > 5.0:
        # Wide SL: suggest reducing position size or risk
        safe_risk_pct = risk_percent * (3.0 / sl_distance_pct)
        warnings.append("SL very wide")
        advice.append(Advice(
            level="warn",
            message=f"SL at {sl_distance_pct:.1f}% — wide stop, large position exposure",
            action=f"Reduce risk to ~{safe_risk_pct:.1f}% to keep position manageable, or tighten SL",
        ))
    elif sl_distance_pct > 3.0:
        advice.append(Advice(
            level="info",
            message=f"SL at {sl_distance_pct:.1f}% — wider than typical",
            action=f"Position is smaller due to wide SL — {position_size:.4f} {size_label}. This is fine for HTF setups",
        ))

    # ── R:R checks ──
    if tp1_rr < 1.0:
        # Suggest a TP1 that gives at least 1R
        min_tp1 = entry + sl_distance if direction == "long" else entry - sl_distance
        warnings.append("TP1 R:R below 1:1")
        advice.append(Advice(
            level="danger",
            message=f"TP1 R:R is {tp1_rr:.1f} — negative expectancy",
            action=f"Move TP1 to at least ${min_tp1:.2f} for 1:1 R:R",
        ))
    elif tp1_rr < 1.5:
        advice.append(Advice(
            level="info",
            message=f"TP1 R:R is {tp1_rr:.1f} — acceptable but tight",
            action="Consider if there's a structural level further away for TP1",
        ))

    if has_tp2 and tp2_rr_val < 2.0:
        min_tp2 = entry + 2 * sl_distance if direction == "long" else entry - 2 * sl_distance
        warnings.append("TP2 R:R below 2:1")
        advice.append(Advice(
            level="warn",
            message=f"TP2 R:R is {tp2_rr_val:.1f} — weak runner target",
            action=f"Move TP2 to ${min_tp2:.2f} for 2:1 or remove TP2 and close 100% at TP1",
        ))

    # ── Margin / exposure checks ──
    if margin_pct > 50.0:
        safe_risk = risk_percent * (40.0 / margin_pct)
        warnings.append("Using >50% of balance as margin")
        advice.append(Advice(
            level="danger",
            message=f"Margin is {margin_pct:.0f}% of balance — liquidation risk",
            action=f"Reduce risk to {safe_risk:.1f}% or lower leverage to {max(1, leverage - 2)}x",
        ))
    elif margin_pct > 30.0:
        advice.append(Advice(
            level="warn",
            message=f"Margin is {margin_pct:.0f}% of balance — heavy allocation",
            action=f"Consider reducing risk% or leverage for more room",
        ))

    # ── Position value vs balance ──
    value_ratio = position_value_usd / balance if balance > 0 else 0
    if value_ratio > 10:
        advice.append(Advice(
            level="warn",
            message=f"Notional {value_ratio:.0f}x your balance (${position_value_usd:.0f})",
            action=f"High notional — one bad fill or spike and you lose more than planned",
        ))

    # ── Good setup confirmation ──
    if not advice and tp1_rr >= 1.5 and 1.0 <= sl_distance_pct <= 3.0 and margin_pct < 30.0:
        advice.append(Advice(
            level="info",
            message=f"Setup looks solid — {tp1_rr:.1f}R TP1, {sl_distance_pct:.1f}% SL, {margin_pct:.0f}% margin",
            action=None,
        ))

    return CalculatorResult(
        pair=pair,
        direction=direction,
        margin_type=margin_type,
        balance=balance,
        risk_percent=risk_percent,
        entry=entry,
        stop_loss=stop_loss,
        take_profit_1=tp1,
        take_profit_2=tp2,
        leverage=leverage,
        risk_usd=round(risk_usd, 2),
        sl_distance=round(sl_distance, 8),
        sl_distance_pct=round(sl_distance_pct, 4),
        position_size=round(position_size, 8),
        position_size_label=size_label,
        position_value_usd=round(position_value_usd, 2),
        margin_required=round(margin_required, 8),
        margin_currency=margin_currency,
        margin_pct_of_balance=round(margin_pct, 2),
        tp_plan=tp_plan,
        total_potential_profit=round(total_profit, 2),
        total_potential_loss=round(total_loss, 2),
        suggested_tp1=round(suggested_tp1, 8),
        suggested_tp2=round(suggested_tp2, 8),
        warnings=warnings,
        advice=advice,
    )


def _calc_linear(
    risk_usd: float, sl_distance: float, entry: float,
    leverage: int, base_coin: str,
) -> tuple[float, float, float, str, str]:
    """Linear (USDT-margined): size in base asset, margin in USDT."""
    position_size = risk_usd / sl_distance
    position_value = position_size * entry
    margin = position_value / leverage
    return position_size, position_value, margin, "USDT", base_coin


def _calc_inverse(
    risk_usd: float, sl_distance: float, sl_distance_pct: float,
    entry: float, leverage: int, base_coin: str,
) -> tuple[float, float, float, str, str]:
    """Inverse (coin-margined): size in USD contracts, margin in coin.

    For inverse perps, PnL_USD = contracts × (close - entry) / entry.
    At SL: loss = contracts × sl_distance / entry = risk_usd
    So: contracts = risk_usd × entry / sl_distance
    Margin (in coin) = contracts / (entry × leverage)
    """
    contracts = risk_usd * entry / sl_distance
    position_value = contracts  # contracts ARE in USD
    margin_coin = contracts / (entry * leverage)
    return contracts, position_value, margin_coin, base_coin, "contracts"


def _pnl(margin_type: str, direction: str, entry: float, close: float, size: float) -> float:
    """Calculate PnL in USD for a given close price.

    Linear:  PnL = size × (close - entry) for long
    Inverse: PnL = contracts × (close - entry) / entry for long
             (non-linear because PnL is in coin, converted at close price)
    """
    if margin_type == "linear":
        if direction == "long":
            return size * (close - entry)
        else:
            return size * (entry - close)
    else:  # inverse
        if direction == "long":
            return size * (close - entry) / entry
        else:
            return size * (entry - close) / entry


def pnl_usd(margin_type: str, direction: str, entry: float, close: float, size: float) -> float:
    """Public PnL function for use by trade_manager."""
    return _pnl(margin_type, direction, entry, close, size)
