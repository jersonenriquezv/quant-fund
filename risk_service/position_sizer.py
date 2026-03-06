"""
Position sizing calculator.

Formula: position_size = (capital * risk_pct) / abs(entry - sl)
Leverage: (position_size * entry) / capital
If leverage exceeds MAX_LEVERAGE, cap position so leverage = MAX_LEVERAGE.
"""

from config.settings import settings


class PositionSizer:
    """Calculates position size and leverage for a trade setup."""

    def calculate(
        self,
        entry: float,
        sl: float,
        capital: float,
        risk_pct: float,
    ) -> tuple[float, float]:
        """Calculate position size and required leverage.

        Args:
            entry: Entry price.
            sl: Stop-loss price.
            capital: Available capital in USDT.
            risk_pct: Fraction of capital to risk (e.g. 0.02 = 2%).

        Returns:
            (position_size_base, leverage) — position in base currency, leverage as float.

        Raises:
            ValueError: If entry == sl, capital <= 0, or risk_pct <= 0.
        """
        if capital <= 0:
            raise ValueError(f"Capital must be positive, got {capital}")
        if risk_pct <= 0:
            raise ValueError(f"Risk percent must be positive, got {risk_pct}")

        distance = abs(entry - sl)
        if distance == 0:
            raise ValueError("Entry and stop-loss cannot be the same price")

        # Risk-based position sizing
        risk_amount = capital * risk_pct
        position_size = risk_amount / distance

        # Leverage required
        notional = position_size * entry
        leverage = notional / capital

        # Cap at MAX_LEVERAGE
        if leverage > settings.MAX_LEVERAGE:
            leverage = float(settings.MAX_LEVERAGE)
            notional = capital * leverage
            position_size = notional / entry

        return position_size, leverage
