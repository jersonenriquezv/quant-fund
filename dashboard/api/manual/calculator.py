"""Position sizing & R:R calculator for manual trades.

Supports two margin types:
- linear (USDT-margined): position_size in base asset, PnL in USDT
- inverse (coin-margined): position_size in USD contracts, PnL in coin → USD

No imports from risk_service — self-contained math.
"""

from dataclasses import dataclass, field


@dataclass
class TPLevel:
    price: float
    rr_ratio: float
    close_pct: float  # e.g. 50.0
    size_to_close: float
    potential_profit_usd: float
    after_action: str | None = None  # e.g. "Move SL to breakeven"


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

    TP strategy: 50/50 split. TP1 = close 50%, move SL to breakeven. TP2 = close remaining 50%.
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
    tp2 = take_profit_2 if take_profit_2 is not None else suggested_tp2

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

    # TP plan: 50/50 split
    half_size = position_size / 2

    tp1_reward = abs(tp1 - entry)
    tp1_rr = tp1_reward / sl_distance
    tp1_profit = _pnl(margin_type, direction, entry, tp1, half_size)

    tp_plan = [
        TPLevel(
            price=tp1,
            rr_ratio=round(tp1_rr, 2),
            close_pct=50.0,
            size_to_close=half_size,
            potential_profit_usd=round(tp1_profit, 2),
            after_action=f"Move SL to breakeven at {entry}",
        ),
    ]

    tp2_profit = 0.0
    if tp2 is not None:
        tp2_reward = abs(tp2 - entry)
        tp2_rr = tp2_reward / sl_distance
        tp2_profit = _pnl(margin_type, direction, entry, tp2, half_size)
        tp_plan.append(
            TPLevel(
                price=tp2,
                rr_ratio=round(tp2_rr, 2),
                close_pct=50.0,
                size_to_close=half_size,
                potential_profit_usd=round(tp2_profit, 2),
            ),
        )

    total_profit = tp1_profit + tp2_profit
    total_loss = risk_usd

    # Warnings
    warnings: list[str] = []
    if tp1_rr < 1.0:
        warnings.append("TP1 R:R below 1:1")
    if tp2 is not None:
        tp2_rr_val = abs(tp2 - entry) / sl_distance
        if tp2_rr_val < 2.0:
            warnings.append("TP2 R:R below 2:1")
    if sl_distance_pct < 1.0:
        warnings.append("SL too tight — likely noise")
    if sl_distance_pct > 5.0:
        warnings.append("SL very wide")
    if margin_pct > 50.0:
        warnings.append("Using >50% of balance as margin")

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
