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
        self._algo_fetch_errors: dict[str, int] = {}

        if settings.OKX_SANDBOX:
            self._exchange.set_sandbox_mode(True)
            logger.info("OrderExecutor initialized in DEMO/SANDBOX mode")
        else:
            logger.info("OrderExecutor initialized in LIVE mode")

        # Set one-way (net) position mode — avoids posSide errors
        try:
            self._exchange.set_position_mode(hedged=False)
            logger.info("Position mode set to one-way (net)")
        except Exception as e:
            if "already" in str(e).lower():
                logger.debug("Position mode already set to one-way")
            else:
                logger.warning(f"Failed to set position mode: {e}")

    def _ccxt_symbol(self, pair: str) -> str:
        """Convert pair format to ccxt symbol. "BTC/USDT" → "BTC/USDT:USDT"."""
        return f"{pair}:USDT"

    async def _run_sync(self, func, *args, **kwargs):
        """Run a sync ccxt call in the default executor."""
        loop = asyncio.get_running_loop()
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
                settings.MARGIN_MODE, symbol, {"lever": leverage}
            )
            logger.info(f"Margin mode set to {settings.MARGIN_MODE}: {pair}")
        except (ccxt.ExchangeError, ccxt.NetworkError) as e:
            err_msg = str(e).lower()
            if "already" in err_msg:
                logger.debug(f"Margin mode already set: {pair} ({e})")
            else:
                # Real failure (e.g. open position prevents mode change)
                logger.error(
                    f"Cannot set margin mode to {settings.MARGIN_MODE}: "
                    f"{pair} — {e}"
                )
                return False

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
    # Price fetching
    # ================================================================

    async def fetch_ticker(self, pair: str) -> Optional[dict]:
        """Fetch current ticker (bid/ask/last) for a pair."""
        symbol = self._ccxt_symbol(pair)
        try:
            ticker = await self._run_sync(
                self._exchange.fetch_ticker, symbol
            )
            return ticker
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"Fetch ticker error: {pair} {e}")
            return None

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
            # NOTE: Do NOT pass reduceOnly here. OKX algo orders (conditional/
            # stop-market) do not support reduceOnly in one-way (net) mode
            # and return error 51205 "Reduce Only is not available."
            # In net mode, a sell against a long inherently reduces the position.
            order = await self._run_sync(
                self._exchange.create_stop_market_order,
                symbol, side, amount, trigger_price
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
        """Fetch order status. Tries regular orders first, then algo orders."""
        symbol = self._ccxt_symbol(pair)
        try:
            order = await self._run_sync(
                self._exchange.fetch_order, order_id, symbol
            )
            return order
        except ccxt.OrderNotFound:
            # SL/TP are algo orders on OKX — try algo endpoint
            return await self._fetch_algo_order(order_id, pair)
        except ccxt.NetworkError as e:
            logger.error(f"Fetch order network error: {pair} order_id={order_id} {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"Fetch order exchange error: {pair} order_id={order_id} {e}")
            return None

    async def _fetch_algo_order(self, order_id: str, pair: str) -> Optional[dict]:
        """Fetch an algo/conditional order (SL/TP) from OKX.

        Uses OKX native REST API directly (privateGetTradeOrdersAlgo*)
        to avoid ccxt v4 compatibility issues with fetch_open_orders
        routing to unsupported fetchCanceledAndClosedOrders.
        """
        inst_id = pair.replace("/", "-") + "-SWAP"
        try:
            # Step 1: Check pending algo orders
            response = await self._run_sync(
                self._exchange.privateGetTradeOrdersAlgoPending,
                {"ordType": "conditional", "instId": inst_id}
            )
            for item in response.get("data", []):
                if item.get("algoId") == order_id:
                    self._algo_fetch_errors.pop(order_id, None)
                    return {"id": order_id, "status": "open",
                            "filled": 0, "average": 0}

            # Step 2: Check triggered/filled algo orders
            response2 = await self._run_sync(
                self._exchange.privateGetTradeOrdersAlgoHistory,
                {"ordType": "conditional", "instId": inst_id, "state": "effective"}
            )
            for item in response2.get("data", []):
                if item.get("algoId") == order_id:
                    self._algo_fetch_errors.pop(order_id, None)
                    return {"id": order_id, "status": "closed",
                            "filled": float(item.get("sz", 0)),
                            "average": float(item.get("avgPx", 0) or 0)}

            # Step 3: Check cancelled algo orders
            response3 = await self._run_sync(
                self._exchange.privateGetTradeOrdersAlgoHistory,
                {"ordType": "conditional", "instId": inst_id, "state": "canceled"}
            )
            for item in response3.get("data", []):
                if item.get("algoId") == order_id:
                    self._algo_fetch_errors.pop(order_id, None)
                    return {"id": order_id, "status": "canceled",
                            "filled": 0, "average": 0}

            self._algo_fetch_errors.pop(order_id, None)
            logger.warning(f"Algo order not found: {order_id}")
            return None
        except Exception as e:
            # Throttle repeated errors — log first occurrence then every ~60s
            count = self._algo_fetch_errors.get(order_id, 0) + 1
            self._algo_fetch_errors[order_id] = count
            if count == 1 or count % 12 == 0:
                logger.error(
                    f"Fetch algo order error (#{count}): {pair} "
                    f"order_id={order_id} {e}"
                )
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
