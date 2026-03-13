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
import time
from typing import Callable, Optional

import ccxt

from config.settings import settings
from shared.logger import setup_logger

logger = setup_logger("execution_service")


class OrderExecutor:
    """Thin ccxt wrapper for OKX order operations."""

    def __init__(self, metrics_callback: Callable | None = None):
        config = {
            "apiKey": settings.OKX_API_KEY,
            "secret": settings.OKX_SECRET,
            "password": settings.OKX_PASSPHRASE,
            "enableRateLimit": True,
        }

        self._exchange = ccxt.okx(config)
        # CRITICAL: ccxt defaults tdMode to 'cross' on every order unless
        # defaultMarginMode is set. This ensures ALL orders use isolated.
        self._exchange.options["defaultMarginMode"] = settings.MARGIN_MODE
        self._algo_fetch_errors: dict[str, int] = {}

        self._metrics_cb = metrics_callback

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

    def _get_contract_size(self, pair: str) -> float:
        """Get the contract size (ctVal) for a pair.

        ETH-USDT-SWAP: ctVal=0.1 (1 contract = 0.1 ETH)
        BTC-USDT-SWAP: ctVal=0.01 (1 contract = 0.01 BTC)
        """
        symbol = self._ccxt_symbol(pair)
        self._exchange.load_markets()
        market = self._exchange.market(symbol)
        ct_val = float(market.get("contractSize", 1))
        return ct_val if ct_val > 0 else 1.0

    def _to_contracts(self, pair: str, base_amount: float) -> float:
        """Convert base currency amount to OKX contracts.

        On OKX SWAP, ccxt 'amount' = number of contracts, NOT base currency.
        """
        ct_val = self._get_contract_size(pair)
        contracts = base_amount / ct_val
        # Round to lot step (lotSz precision)
        symbol = self._ccxt_symbol(pair)
        market = self._exchange.market(symbol)
        precision = market.get("precision", {}).get("amount", 0.01)
        if precision > 0:
            contracts = round(contracts / precision) * precision
            contracts = round(contracts, 8)  # avoid float artifacts
        return contracts

    def contracts_to_base(self, pair: str, contracts: float) -> float:
        """Convert OKX contracts back to base currency.

        Used to normalize ccxt 'filled' values (which are in contracts)
        back to base currency (ETH/BTC) for the rest of the system.
        """
        ct_val = self._get_contract_size(pair)
        return contracts * ct_val

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
            # CRITICAL: pass marginMode explicitly — ccxt defaults to "cross"
            # if not specified, which would override the set_margin_mode call above.
            await self._run_sync(
                self._exchange.set_leverage, leverage, symbol,
                {"mgnMode": settings.MARGIN_MODE}
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
        self, pair: str, side: str, amount: float, price: float,
        sl_trigger_price: float = 0, tp_price: float = 0,
    ) -> Optional[dict]:
        """Place a limit entry order with optional attached SL/TP.

        When sl_trigger_price and tp_price are provided, OKX attaches algo
        orders that auto-activate when the entry fills. This guarantees
        SL/TP exist on the exchange even if the bot crashes after fill.

        Args:
            amount: Size in BASE currency (ETH/BTC). Converted to contracts internally.
            sl_trigger_price: If > 0, attach a stop-market SL at this price.
            tp_price: If > 0, attach a limit TP at this price.
        """
        symbol = self._ccxt_symbol(pair)
        contracts = self._to_contracts(pair, amount)
        params: dict = {}
        if sl_trigger_price > 0:
            params["stopLoss"] = {"triggerPrice": sl_trigger_price}
        if tp_price > 0:
            params["takeProfit"] = {
                "triggerPrice": tp_price,
                "price": tp_price,
            }
        try:
            t0 = time.monotonic()
            order = await self._run_sync(
                self._exchange.create_order,
                symbol, "limit", side, contracts, price, params
            )
            if self._metrics_cb:
                self._metrics_cb("okx_order_latency_ms", (time.monotonic() - t0) * 1000, pair, {"type": "limit"})
            attached = ""
            if sl_trigger_price > 0 or tp_price > 0:
                attached = f" attached_sl={sl_trigger_price:.2f} attached_tp={tp_price:.2f}"
            logger.info(
                f"Limit order placed: {pair} {side} base={amount:.6f} "
                f"contracts={contracts} price={price:.2f} "
                f"order_id={order.get('id')}{attached}"
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
            amount: Size in BASE currency (ETH/BTC). Converted to contracts internally.
            side: The closing side ("sell" for long SL, "buy" for short SL).
        """
        symbol = self._ccxt_symbol(pair)
        contracts = self._to_contracts(pair, amount)
        try:
            # NOTE: Do NOT pass reduceOnly here. OKX algo orders (conditional/
            # stop-market) do not support reduceOnly in one-way (net) mode
            # and return error 51205 "Reduce Only is not available."
            # In net mode, a sell against a long inherently reduces the position.
            t0 = time.monotonic()
            order = await self._run_sync(
                self._exchange.create_stop_market_order,
                symbol, side, contracts, trigger_price
            )
            if self._metrics_cb:
                self._metrics_cb("okx_order_latency_ms", (time.monotonic() - t0) * 1000, pair, {"type": "stop_market"})
            logger.info(
                f"Stop-market placed: {pair} {side} base={amount:.6f} "
                f"contracts={contracts} trigger={trigger_price:.2f} order_id={order.get('id')}"
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
            amount: Size in BASE currency (ETH/BTC). Converted to contracts internally.
            side: The closing side ("sell" for long TP, "buy" for short TP).
        """
        symbol = self._ccxt_symbol(pair)
        contracts = self._to_contracts(pair, amount)
        try:
            params = {"reduceOnly": True}
            t0 = time.monotonic()
            order = await self._run_sync(
                self._exchange.create_order,
                symbol, "limit", side, contracts, price, params
            )
            if self._metrics_cb:
                self._metrics_cb("okx_order_latency_ms", (time.monotonic() - t0) * 1000, pair, {"type": "take_profit"})
            logger.info(
                f"Take-profit placed: {pair} {side} base={amount:.6f} "
                f"contracts={contracts} price={price:.2f} order_id={order.get('id')}"
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
        """Cancel an open order. Tries regular cancel first, then algo cancel.

        OKX has separate endpoints for regular vs algo orders (trigger/conditional).
        Stop-market orders (SL) are algo orders and need the algo cancel endpoint.
        """
        symbol = self._ccxt_symbol(pair)
        try:
            await self._run_sync(
                self._exchange.cancel_order, order_id, symbol
            )
            logger.info(f"Order cancelled: {pair} order_id={order_id}")
            return True
        except ccxt.OrderNotFound:
            # Regular cancel failed — try algo cancel (SL/TP are algo orders)
            return await self.cancel_algo_order(order_id, pair)
        except ccxt.NetworkError as e:
            logger.error(f"Cancel network error: {pair} order_id={order_id} {e}")
            return False
        except ccxt.ExchangeError as e:
            logger.error(f"Cancel exchange error: {pair} order_id={order_id} {e}")
            return False

    async def cancel_algo_order(self, order_id: str, pair: str) -> bool:
        """Cancel an algo order (trigger/conditional) on OKX.

        Uses OKX native REST endpoint POST /api/v5/trade/cancel-algos.
        """
        inst_id = pair.replace("/", "-") + "-SWAP"
        try:
            result = await self._run_sync(
                self._exchange.privatePostTradeCancelAlgos,
                [{"instId": inst_id, "algoId": order_id}]
            )
            code = result.get("code", "")
            if code == "0":
                logger.info(f"Algo order cancelled: {pair} algoId={order_id}")
                return True
            else:
                msg = result.get("msg", "")
                logger.warning(f"Algo cancel failed: {pair} algoId={order_id} code={code} msg={msg}")
                return False
        except Exception as e:
            logger.warning(f"Algo cancel error: {pair} algoId={order_id} {e}")
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
        """Fetch an algo order (SL/TP) from OKX.

        OKX stop-market orders (SL) are created as 'trigger' type algo orders.
        Queries both 'trigger' and 'conditional' types to cover all cases.
        """
        inst_id = pair.replace("/", "-") + "-SWAP"
        # OKX algo order types used by our bot:
        # - "trigger": stop-market orders (SL) created via create_stop_market_order
        # - "conditional": some older or TP-style algo orders
        algo_types = ["trigger", "conditional"]
        try:
            for ord_type in algo_types:
                # Step 1: Check pending algo orders
                response = await self._run_sync(
                    self._exchange.privateGetTradeOrdersAlgoPending,
                    {"ordType": ord_type, "instId": inst_id}
                )
                for item in response.get("data", []):
                    if item.get("algoId") == order_id:
                        self._algo_fetch_errors.pop(order_id, None)
                        return {"id": order_id, "status": "open",
                                "filled": 0, "average": 0}

                # Step 2: Check triggered/filled algo orders
                response2 = await self._run_sync(
                    self._exchange.privateGetTradeOrdersAlgoHistory,
                    {"ordType": ord_type, "instId": inst_id, "state": "effective"}
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
                    {"ordType": ord_type, "instId": inst_id, "state": "canceled"}
                )
                for item in response3.get("data", []):
                    if item.get("algoId") == order_id:
                        self._algo_fetch_errors.pop(order_id, None)
                        return {"id": order_id, "status": "canceled",
                                "filled": 0, "average": 0}

            self._algo_fetch_errors.pop(order_id, None)
            logger.warning(f"Algo order not found in any type: {order_id}")
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
    # Attached algo order discovery
    # ================================================================

    async def find_pending_algo_orders(self, pair: str) -> list[dict]:
        """Find pending algo orders for a pair (trigger + conditional).

        Used after entry fill to discover SL/TP attached to the entry order.
        OKX puts attached SL orders under 'conditional' type, not 'trigger'.
        Returns raw OKX algo order dicts with algoId, triggerPx, slTriggerPx, etc.
        """
        inst_id = pair.replace("/", "-") + "-SWAP"
        all_orders: list[dict] = []
        for ord_type in ("trigger", "conditional"):
            try:
                response = await self._run_sync(
                    self._exchange.privateGetTradeOrdersAlgoPending,
                    {"ordType": ord_type, "instId": inst_id}
                )
                all_orders.extend(response.get("data", []))
            except Exception as e:
                logger.error(f"Find pending algo orders ({ord_type}) error: {pair} {e}")
        return all_orders

    # ================================================================
    # Emergency
    # ================================================================

    async def close_position_market(
        self, pair: str, side: str, amount: float
    ) -> Optional[dict]:
        """Emergency market close. Used when SL placement fails after entry fill.

        Args:
            amount: Size in BASE currency (ETH/BTC). Converted to contracts internally.
        """
        symbol = self._ccxt_symbol(pair)
        contracts = self._to_contracts(pair, amount)
        try:
            params = {"reduceOnly": True}
            order = await self._run_sync(
                self._exchange.create_order,
                symbol, "market", side, contracts, None, params
            )
            logger.warning(
                f"EMERGENCY market close: {pair} {side} base={amount:.6f} "
                f"contracts={contracts} order_id={order.get('id')}"
            )
            return order
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder,
                ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"EMERGENCY close FAILED: {pair} {side} {e}")
            return None

    # Sentinel returned by fetch_position when API succeeds but no position exists.
    # Callers can distinguish from None (network error).
    POSITION_EMPTY: dict = {}

    async def fetch_position(self, pair: str) -> Optional[dict]:
        """Fetch open position for a pair.

        Returns:
            dict with position data if open position exists,
            POSITION_EMPTY ({}) if API succeeded but no position found,
            None on network/exchange error.
        """
        symbol = self._ccxt_symbol(pair)
        try:
            positions = await self._run_sync(
                self._exchange.fetch_positions, [symbol]
            )
            for pos in positions:
                if pos.get("symbol") == symbol and float(pos.get("contracts", 0)) > 0:
                    return pos
            return self.POSITION_EMPTY
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(f"Fetch position error: {pair} {e}")
            return None
