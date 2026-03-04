"""
OKX order executor via ccxt — thin wrapper for order placement.

Follows same patterns as data_service/exchange_client.py:
- Same ccxt config (apiKey, secret, password, enableRateLimit, sandbox)
- Same symbol conversion: "BTC/USDT" → "BTC/USDT:USDT"
- All calls async via loop.run_in_executor() (ccxt is sync)
- Returns None on failure — caller decides what to do

Does NOT contain business logic. Just places/cancels/fetches orders.
"""

import asyncio
from typing import Optional

import ccxt

from config.settings import settings
from shared.logger import setup_logger

logger = setup_logger("execution_service")


class OrderExecutor:
    """Thin ccxt wrapper for OKX order operations."""

    def __init__(self):
        config = {
            "apiKey": settings.OKX_API_KEY,
            "secret": settings.OKX_SECRET,
            "password": settings.OKX_PASSPHRASE,
            "enableRateLimit": True,
        }

        self._exchange = ccxt.okx(config)

        if settings.OKX_SANDBOX:
            self._exchange.set_sandbox_mode(True)
            logger.info("OrderExecutor initialized in DEMO/SANDBOX mode")
        else:
            logger.info("OrderExecutor initialized in LIVE mode")

    def _ccxt_symbol(self, pair: str) -> str:
        """Convert pair format to ccxt symbol. "BTC/USDT" → "BTC/USDT:USDT"."""
        return f"{pair}:USDT"

    async def _run_sync(self, func, *args, **kwargs):
        """Run a sync ccxt call in the default executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    # ================================================================
    # Pair configuration
    # ================================================================

    async def configure_pair(self, pair: str, leverage: int) -> bool:
        """Set isolated margin mode and leverage before trading.

        Returns True on success, False on failure.
        """
        symbol = self._ccxt_symbol(pair)
        try:
            await self._run_sync(
                self._exchange.set_margin_mode,
                settings.MARGIN_MODE, symbol
            )
        except (ccxt.ExchangeError, ccxt.NetworkError) as e:
            # "already set" is not a real error
            if "already" not in str(e).lower():
                logger.warning(f"Set margin mode failed: {pair} {e}")

        try:
            await self._run_sync(
                self._exchange.set_leverage, leverage, symbol
            )
            logger.info(f"Pair configured: {pair} margin={settings.MARGIN_MODE} leverage={leverage}x")
            return True
        except ccxt.InsufficientFunds as e:
            logger.error(f"Configure pair insufficient funds: {pair} {e}")
            return False
        except ccxt.ExchangeError as e:
            logger.error(f"Configure pair exchange error: {pair} {e}")
            return False
        except ccxt.NetworkError as e:
            logger.error(f"Configure pair network error: {pair} {e}")
            return False

    # ================================================================
    # Order placement
    # ================================================================

    async def place_limit_order(
        self, pair: str, side: str, amount: float, price: float
    ) -> Optional[dict]:
        """Place a limit entry order. Returns order dict or None on failure."""
        symbol = self._ccxt_symbol(pair)
        try:
            order = await self._run_sync(
                self._exchange.create_order,
                symbol, "limit", side, amount, price
            )
            logger.info(
                f"Limit order placed: {pair} {side} amount={amount:.6f} "
                f"price={price:.2f} order_id={order.get('id')}"
            )
            return order
        except ccxt.InsufficientFunds as e:
            logger.error(f"Limit order insufficient funds: {pair} {side} {e}")
            return None
        except ccxt.InvalidOrder as e:
            logger.error(f"Limit order invalid: {pair} {side} {e}")
            return None
        except ccxt.NetworkError as e:
            logger.error(f"Limit order network error: {pair} {side} {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"Limit order exchange error: {pair} {side} {e}")
            return None
        except ccxt.RateLimitExceeded as e:
            logger.warning(f"Limit order rate limited: {pair} {side} {e}")
            return None

    async def place_stop_market(
        self, pair: str, side: str, amount: float, trigger_price: float
    ) -> Optional[dict]:
        """Place a stop-market order (for SL). NOT stop-limit — guaranteed fill.

        Args:
            side: The closing side ("sell" for long SL, "buy" for short SL).
        """
        symbol = self._ccxt_symbol(pair)
        try:
            params = {"reduceOnly": True, "triggerPrice": trigger_price}
            order = await self._run_sync(
                self._exchange.create_order,
                symbol, "market", side, amount, None, params
            )
            logger.info(
                f"Stop-market placed: {pair} {side} amount={amount:.6f} "
                f"trigger={trigger_price:.2f} order_id={order.get('id')}"
            )
            return order
        except ccxt.InsufficientFunds as e:
            logger.error(f"Stop-market insufficient funds: {pair} {side} {e}")
            return None
        except ccxt.InvalidOrder as e:
            logger.error(f"Stop-market invalid: {pair} {side} {e}")
            return None
        except ccxt.NetworkError as e:
            logger.error(f"Stop-market network error: {pair} {side} {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"Stop-market exchange error: {pair} {side} {e}")
            return None
        except ccxt.RateLimitExceeded as e:
            logger.warning(f"Stop-market rate limited: {pair} {side} {e}")
            return None

    async def place_take_profit(
        self, pair: str, side: str, amount: float, price: float
    ) -> Optional[dict]:
        """Place a limit TP order (reduceOnly).

        Args:
            side: The closing side ("sell" for long TP, "buy" for short TP).
        """
        symbol = self._ccxt_symbol(pair)
        try:
            params = {"reduceOnly": True}
            order = await self._run_sync(
                self._exchange.create_order,
                symbol, "limit", side, amount, price, params
            )
            logger.info(
                f"Take-profit placed: {pair} {side} amount={amount:.6f} "
                f"price={price:.2f} order_id={order.get('id')}"
            )
            return order
        except ccxt.InsufficientFunds as e:
            logger.error(f"Take-profit insufficient funds: {pair} {side} {e}")
            return None
        except ccxt.InvalidOrder as e:
            logger.error(f"Take-profit invalid: {pair} {side} {e}")
            return None
        except ccxt.NetworkError as e:
            logger.error(f"Take-profit network error: {pair} {side} {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"Take-profit exchange error: {pair} {side} {e}")
            return None
        except ccxt.RateLimitExceeded as e:
            logger.warning(f"Take-profit rate limited: {pair} {side} {e}")
            return None

    # ================================================================
    # Order management
    # ================================================================

    async def cancel_order(self, order_id: str, pair: str) -> bool:
        """Cancel an open order. Returns True on success."""
        symbol = self._ccxt_symbol(pair)
        try:
            await self._run_sync(
                self._exchange.cancel_order, order_id, symbol
            )
            logger.info(f"Order cancelled: {pair} order_id={order_id}")
            return True
        except ccxt.OrderNotFound:
            logger.warning(f"Cancel: order not found (may already be filled): {order_id}")
            return False
        except ccxt.NetworkError as e:
            logger.error(f"Cancel network error: {pair} order_id={order_id} {e}")
            return False
        except ccxt.ExchangeError as e:
            logger.error(f"Cancel exchange error: {pair} order_id={order_id} {e}")
            return False

    async def fetch_order(self, order_id: str, pair: str) -> Optional[dict]:
        """Fetch order status. Returns order dict or None."""
        symbol = self._ccxt_symbol(pair)
        try:
            order = await self._run_sync(
                self._exchange.fetch_order, order_id, symbol
            )
            return order
        except ccxt.OrderNotFound:
            logger.warning(f"Fetch: order not found: {order_id}")
            return None
        except ccxt.NetworkError as e:
            logger.error(f"Fetch order network error: {pair} order_id={order_id} {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"Fetch order exchange error: {pair} order_id={order_id} {e}")
            return None

    # ================================================================
    # Emergency
    # ================================================================

    async def close_position_market(
        self, pair: str, side: str, amount: float
    ) -> Optional[dict]:
        """Emergency market close. Used when SL placement fails after entry fill."""
        symbol = self._ccxt_symbol(pair)
        try:
            params = {"reduceOnly": True}
            order = await self._run_sync(
                self._exchange.create_order,
                symbol, "market", side, amount, None, params
            )
            logger.warning(
                f"EMERGENCY market close: {pair} {side} amount={amount:.6f} "
                f"order_id={order.get('id')}"
            )
            return order
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder,
                ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"EMERGENCY close FAILED: {pair} {side} {e}")
            return None

    async def fetch_position(self, pair: str) -> Optional[dict]:
        """Fetch open position for a pair. Returns position dict or None."""
        symbol = self._ccxt_symbol(pair)
        try:
            positions = await self._run_sync(
                self._exchange.fetch_positions, [symbol]
            )
            for pos in positions:
                if pos.get("symbol") == symbol and float(pos.get("contracts", 0)) > 0:
                    return pos
            return None
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"Fetch position error: {pair} {e}")
            return None
