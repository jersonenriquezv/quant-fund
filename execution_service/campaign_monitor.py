"""
Campaign monitor — background async loop managing HTF position campaigns.

A campaign is a multi-day position trade on 4H timeframe with pyramid adds
and trailing SL on 4H swing lows (longs) / swing highs (shorts).

Lifecycle:
    pending_initial ──[fill]──> active        (place SL, no TP)
    pending_initial ──[timeout]> closed       (cancel entry)
    active ──[add fill]───────> active        (update SL for total size)
    active ──[SL fills]───────> closed        (trailing SL hit)
    active ──[timeout 7d]─────> closed        (max duration)

Key differences from intraday PositionMonitor:
- No TP orders — exit via trailing SL only
- Pyramid adds: up to 3 adds with decreasing margin
- SL trails on 4H swing lows (longs) / swing highs (shorts)
- One SL order covers total stacked position (OKX net mode)
"""

import asyncio
import json
import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from execution_service.models import PositionCampaign, CampaignAdd
from execution_service.executor import OrderExecutor

logger = setup_logger("execution_service")


class CampaignMonitor:
    """Manages a single HTF campaign through its lifecycle."""

    def __init__(self, executor: OrderExecutor, risk_service,
                 strategy_service=None, data_store=None,
                 alert_manager=None) -> None:
        self._executor = executor
        self._risk = risk_service
        self._strategy = strategy_service
        self._data_store = data_store
        self._alert_manager = alert_manager
        self._campaign: Optional[PositionCampaign] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def campaign(self) -> Optional[PositionCampaign]:
        return self._campaign

    def has_active_campaign(self, pair: str | None = None) -> bool:
        """Check if there's an active campaign, optionally for a specific pair."""
        if self._campaign is None:
            return False
        if self._campaign.phase == "closed":
            return False
        if pair is not None:
            return self._campaign.pair == pair
        return True

    # ================================================================
    # Lifecycle
    # ================================================================

    def start(self) -> None:
        """Start the campaign monitor poll loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="campaign_monitor")
        logger.info("Campaign monitor started")

    async def stop(self) -> None:
        """Stop the monitor. Cancel unfilled entries."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Cancel pending initial entry
        if self._campaign and self._campaign.phase == "pending_initial":
            if self._campaign.initial_order_id:
                await self._executor.cancel_order(
                    self._campaign.initial_order_id, self._campaign.pair
                )
            self._close_campaign("cancelled")

        logger.info("Campaign monitor stopped")

    # ================================================================
    # Execute new campaign
    # ================================================================

    async def execute_campaign(self, setup, ai_confidence: float) -> bool:
        """Create a new campaign and place initial limit entry order.

        Args:
            setup: TradeSetup from evaluate_htf()
            ai_confidence: AI confidence score

        Returns True if entry order placed successfully.
        """
        if self._campaign is not None and self._campaign.phase != "closed":
            logger.warning("Cannot execute campaign — one already active")
            return False

        leverage = float(settings.MAX_LEVERAGE)
        margin = settings.HTF_INITIAL_MARGIN
        notional = margin * leverage
        position_size = notional / setup.entry_price

        # Check exchange minimum
        min_size = settings.MIN_ORDER_SIZES.get(setup.pair, 0)
        if min_size > 0 and position_size < min_size:
            logger.warning(
                f"HTF campaign: position size {position_size:.6f} below minimum "
                f"{min_size} for {setup.pair}"
            )
            return False

        # Configure pair
        configured = await self._executor.configure_pair(setup.pair, int(leverage))
        if not configured:
            logger.error(f"HTF campaign: failed to configure {setup.pair}")
            return False

        # Place limit entry
        side = "buy" if setup.direction == "long" else "sell"
        sl_price = setup.sl_price

        # Sandbox: use current price
        if settings.OKX_SANDBOX:
            ticker = await self._executor.fetch_ticker(setup.pair)
            if ticker is None:
                return False
            ask, bid = ticker.get("ask"), ticker.get("bid")
            if not ask or not bid:
                return False
            tolerance = settings.SANDBOX_LIMIT_TOLERANCE_PCT
            entry_price = ask * (1 + tolerance) if side == "buy" else bid * (1 - tolerance)
        else:
            entry_price = setup.entry_price

        # No TP for campaigns — exit via trailing SL only
        order = await self._executor.place_limit_order(
            setup.pair, side, position_size, entry_price,
            sl_trigger_price=sl_price,
        )

        if order is None:
            logger.error(f"HTF campaign: entry order failed for {setup.pair}")
            return False

        # Notify risk service
        if self._risk is not None:
            self._risk.on_trade_opened(
                setup.pair, setup.direction, setup.entry_price, int(time.time())
            )

        campaign = PositionCampaign(
            pair=setup.pair,
            direction=setup.direction,
            phase="pending_initial",
            initial_entry_price=setup.entry_price,
            initial_sl_price=setup.sl_price,
            initial_order_id=order.get("id"),
            initial_setup_type=setup.setup_type,
            initial_margin=margin,
            ai_confidence=ai_confidence,
            htf_bias=setup.htf_bias,
            leverage=leverage,
            created_at=int(time.time()),
        )

        self._campaign = campaign
        self._update_campaign_cache()

        logger.info(
            f"HTF campaign created: {setup.pair} {setup.direction} "
            f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
            f"margin=${margin} leverage={leverage}x "
            f"campaign_id={campaign.campaign_id}"
        )

        # Telegram notification
        if self._alert_manager is not None:
            self._safe_notify(
                self._alert_manager.notify_order_placed(setup, None)
            )

        return True

    # ================================================================
    # Poll loop
    # ================================================================

    async def _poll_loop(self) -> None:
        """Poll campaign status at regular intervals."""
        while self._running:
            try:
                if self._campaign and self._campaign.phase != "closed":
                    await self._check_campaign()
            except Exception as e:
                logger.error(f"Campaign monitor poll error: {e}")
            await asyncio.sleep(settings.ORDER_POLL_INTERVAL)

    async def _check_campaign(self) -> None:
        """Advance campaign state machine."""
        c = self._campaign
        if c is None:
            return

        if c.phase == "pending_initial":
            await self._check_initial_entry(c)
        elif c.phase == "active":
            await self._check_active_campaign(c)

    # ================================================================
    # State: pending_initial
    # ================================================================

    async def _check_initial_entry(self, c: PositionCampaign) -> None:
        """Check if initial entry order filled or timed out."""
        now = int(time.time())

        if now - c.created_at >= settings.HTF_ENTRY_TIMEOUT_SECONDS:
            logger.info(f"HTF campaign entry timeout: {c.pair}")
            if c.initial_order_id:
                await self._executor.cancel_order(c.initial_order_id, c.pair)
            self._close_campaign("cancelled")
            return

        if not c.initial_order_id:
            return

        order = await self._executor.fetch_order(c.initial_order_id, c.pair)
        if order is None:
            return

        status = order.get("status", "")
        filled_contracts = float(order.get("filled", 0))
        filled = self._executor.contracts_to_base(c.pair, filled_contracts)

        if status == "closed" and filled > 0:
            actual_price = float(order.get("average", 0) or order.get("price", 0))
            await self._on_initial_filled(c, actual_price, filled)
        elif status in ("canceled", "cancelled"):
            if filled > 0:
                actual_price = float(order.get("average", 0) or order.get("price", 0))
                await self._on_initial_filled(c, actual_price, filled)
            else:
                self._close_campaign("cancelled")

    async def _on_initial_filled(self, c: PositionCampaign,
                                 actual_price: float, filled_size: float) -> None:
        """Initial entry filled — record fill, place SL for full size."""
        c.actual_initial_entry = actual_price
        c.initial_size = filled_size
        c.total_size = filled_size
        c.total_margin = c.initial_margin
        c.weighted_entry = actual_price
        c.filled_at = int(time.time())

        close_side = "sell" if c.direction == "long" else "buy"

        # Try to find attached SL from the entry order
        sl_found = False
        await asyncio.sleep(0.5)
        algos = await self._executor.find_pending_algo_orders(c.pair)
        for algo in algos:
            trigger_px = float(algo.get("slTriggerPx", 0) or algo.get("triggerPx", 0) or 0)
            if trigger_px > 0 and c.initial_sl_price > 0:
                diff_pct = abs(trigger_px - c.initial_sl_price) / c.initial_sl_price
                if diff_pct < 0.005:
                    c.sl_order_id = algo.get("algoId", "")
                    c.current_sl_price = trigger_px
                    sl_found = True
                    break

        if not sl_found:
            # Place SL manually
            sl_order = await self._executor.place_stop_market(
                c.pair, close_side, filled_size, c.initial_sl_price
            )
            if sl_order is None:
                logger.error(f"HTF campaign: SL placement FAILED — emergency close: {c.pair}")
                result = await self._executor.close_position_market(
                    c.pair, close_side, filled_size
                )
                self._close_campaign("emergency")
                return
            c.sl_order_id = sl_order.get("id")
            c.current_sl_price = c.initial_sl_price

        c.phase = "active"
        self._update_campaign_cache()
        self._persist_campaign_open(c)

        logger.info(
            f"HTF campaign ACTIVE: {c.pair} {c.direction} "
            f"entry={actual_price:.2f} size={filled_size:.6f} "
            f"sl={c.current_sl_price:.2f} campaign_id={c.campaign_id}"
        )

    # ================================================================
    # State: active
    # ================================================================

    async def _check_active_campaign(self, c: PositionCampaign) -> None:
        """Check SL, trailing SL, pending adds, timeout."""
        now = int(time.time())

        # Max duration timeout (7 days)
        created = c.filled_at or c.created_at
        if now - created >= settings.HTF_MAX_CAMPAIGN_DURATION:
            logger.info(f"HTF campaign duration timeout: {c.pair}")
            await self._close_all_and_market_close(c)
            return

        # Check SL
        if c.sl_order_id:
            sl_status = await self._executor.fetch_order(c.sl_order_id, c.pair)
            if sl_status and sl_status.get("status") == "closed":
                logger.info(f"HTF campaign SL hit: {c.pair} {c.direction}")
                self._calculate_pnl(c, c.current_sl_price)
                self._close_campaign("trailing_sl")
                return
            if sl_status and sl_status.get("status") == "canceled":
                # SL cancelled externally — re-place
                logger.warning(f"HTF campaign SL cancelled externally: {c.pair}")
                close_side = "sell" if c.direction == "long" else "buy"
                new_sl = await self._executor.place_stop_market(
                    c.pair, close_side, c.total_size, c.current_sl_price
                )
                if new_sl:
                    c.sl_order_id = new_sl.get("id")
                    c.sl_fetch_failures = 0
                return
            if sl_status is None:
                c.sl_fetch_failures += 1
                if c.sl_fetch_failures >= 12:
                    await self._handle_sl_vanished(c)
                    return
            else:
                c.sl_fetch_failures = 0

        # Check pending add
        if c.pending_add is not None:
            await self._check_pending_add(c)

        # Trailing SL on 4H swing levels
        await self._check_trailing_sl(c)

    # ================================================================
    # Trailing SL — 4H swing lows (long) / swing highs (short)
    # ================================================================

    async def _check_trailing_sl(self, c: PositionCampaign) -> None:
        """Trail SL on 4H swing levels. Only moves SL up (long) or down (short)."""
        if self._strategy is None:
            return

        swing_highs, swing_lows = self._strategy.get_htf_swing_levels(c.pair)

        if c.direction == "long" and swing_lows:
            # Trail on most recent swing low above current SL
            recent_lows = sorted(swing_lows, key=lambda s: s.timestamp, reverse=True)
            for sl in recent_lows:
                if sl.price > c.current_sl_price and sl.price < c.weighted_entry:
                    # New swing low is higher than current SL — trail up
                    logger.info(
                        f"HTF campaign trailing SL: {c.pair} "
                        f"{c.current_sl_price:.2f} → {sl.price:.2f} "
                        f"(4H swing low)"
                    )
                    await self._adjust_campaign_sl(c, sl.price)
                    break

        elif c.direction == "short" and swing_highs:
            recent_highs = sorted(swing_highs, key=lambda s: s.timestamp, reverse=True)
            for sh in recent_highs:
                if sh.price < c.current_sl_price and sh.price > c.weighted_entry:
                    logger.info(
                        f"HTF campaign trailing SL: {c.pair} "
                        f"{c.current_sl_price:.2f} → {sh.price:.2f} "
                        f"(4H swing high)"
                    )
                    await self._adjust_campaign_sl(c, sh.price)
                    break

    # ================================================================
    # Pyramid adds
    # ================================================================

    async def evaluate_add(self, c: PositionCampaign, candle) -> None:
        """Evaluate whether to add to the campaign position.

        Conditions:
        1. Number of adds < HTF_MAX_ADDS
        2. Campaign is profitable (>= HTF_ADD_MIN_RR from initial entry)
        3. New setup found in same direction on signal timeframe
        """
        if c.pending_add is not None:
            return  # Already waiting for an add to fill

        if len(c.adds) >= settings.HTF_MAX_ADDS:
            return

        # Check profitability
        if not c.actual_initial_entry or not c.initial_sl_price:
            return
        risk = abs(c.actual_initial_entry - c.initial_sl_price)
        if risk <= 0:
            return

        ticker = await self._executor.fetch_ticker(c.pair)
        if ticker is None:
            return
        current_price = float(ticker.get("last", 0) or 0)
        if current_price <= 0:
            return

        if c.direction == "long":
            profit = current_price - c.actual_initial_entry
        else:
            profit = c.actual_initial_entry - current_price

        current_rr = profit / risk
        if current_rr < settings.HTF_ADD_MIN_RR:
            return

        # Look for new setup in same direction
        if self._strategy is None:
            return
        setup = self._strategy.evaluate_htf(c.pair, candle)
        if setup is None:
            return
        if setup.direction != c.direction:
            return

        # Determine add margin
        add_number = len(c.adds) + 1
        margin = c.get_add_margin(add_number)
        if margin <= 0:
            return

        notional = margin * c.leverage
        add_size = notional / setup.entry_price

        # Check exchange minimum
        min_size = settings.MIN_ORDER_SIZES.get(c.pair, 0)
        if min_size > 0 and add_size < min_size:
            logger.debug(f"HTF add size too small: {add_size:.6f} < {min_size}")
            return

        # Place limit order for add
        side = "buy" if c.direction == "long" else "sell"
        if settings.OKX_SANDBOX:
            t = await self._executor.fetch_ticker(c.pair)
            if t is None:
                return
            tol = settings.SANDBOX_LIMIT_TOLERANCE_PCT
            entry_price = (t.get("ask", 0) * (1 + tol) if side == "buy"
                           else t.get("bid", 0) * (1 - tol))
        else:
            entry_price = setup.entry_price

        order = await self._executor.place_limit_order(
            c.pair, side, add_size, entry_price,
        )
        if order is None:
            logger.warning(f"HTF campaign add order failed: {c.pair}")
            return

        add = CampaignAdd(
            add_number=add_number,
            margin=margin,
            entry_price=setup.entry_price,
            order_id=order.get("id"),
            setup_type=setup.setup_type,
            placed_at=int(time.time()),
        )
        c.pending_add = add

        logger.info(
            f"HTF campaign add #{add_number} placed: {c.pair} "
            f"margin=${margin} entry={entry_price:.2f} R:R={current_rr:.1f}"
        )

    async def _check_pending_add(self, c: PositionCampaign) -> None:
        """Check if a pending pyramid add order has filled."""
        add = c.pending_add
        if add is None or not add.order_id:
            return

        now = int(time.time())

        # Add timeout: 4 hours
        if now - add.placed_at > 14400:
            logger.info(f"HTF add #{add.add_number} timed out: {c.pair}")
            await self._executor.cancel_order(add.order_id, c.pair)
            c.pending_add = None
            return

        order = await self._executor.fetch_order(add.order_id, c.pair)
        if order is None:
            return

        status = order.get("status", "")
        filled_contracts = float(order.get("filled", 0))
        filled = self._executor.contracts_to_base(c.pair, filled_contracts)

        if status == "closed" and filled > 0:
            actual_price = float(order.get("average", 0) or order.get("price", 0))
            add.actual_entry_price = actual_price
            add.size = filled
            add.filled = True
            add.filled_at = int(time.time())

            c.adds.append(add)
            c.pending_add = None
            c.total_margin += add.margin
            c.update_weighted_entry()

            # Replace SL for new total size
            await self._replace_sl_for_total_size(c)
            self._update_campaign_cache()

            logger.info(
                f"HTF campaign add #{add.add_number} FILLED: {c.pair} "
                f"price={actual_price:.2f} total_size={c.total_size:.6f} "
                f"weighted_entry={c.weighted_entry:.2f}"
            )

        elif status in ("canceled", "cancelled"):
            if filled > 0:
                actual_price = float(order.get("average", 0) or order.get("price", 0))
                add.actual_entry_price = actual_price
                add.size = filled
                add.filled = True
                add.filled_at = int(time.time())
                c.adds.append(add)
                c.total_margin += add.margin
                c.update_weighted_entry()
                await self._replace_sl_for_total_size(c)
                self._update_campaign_cache()
            c.pending_add = None

    # ================================================================
    # SL management
    # ================================================================

    async def _adjust_campaign_sl(self, c: PositionCampaign,
                                  new_price: float) -> None:
        """Move campaign SL. Place new before cancelling old (zero gap)."""
        close_side = "sell" if c.direction == "long" else "buy"

        new_sl = await self._executor.place_stop_market(
            c.pair, close_side, c.total_size, new_price
        )
        if new_sl is None:
            logger.error(f"HTF campaign: new SL placement failed — keeping old: {c.pair}")
            return

        old_id = c.sl_order_id
        if old_id:
            await self._executor.cancel_order(old_id, c.pair)

        c.sl_order_id = new_sl.get("id")
        c.current_sl_price = new_price
        c.sl_fetch_failures = 0
        self._update_campaign_cache()

    async def _replace_sl_for_total_size(self, c: PositionCampaign) -> None:
        """Replace SL order to cover new total position size after an add."""
        await self._adjust_campaign_sl(c, c.current_sl_price)

    async def _handle_sl_vanished(self, c: PositionCampaign) -> None:
        """Fallback when SL can't be found for 60+ seconds."""
        exchange_pos = await self._executor.fetch_position(c.pair)
        if exchange_pos is None:
            return

        contracts = float(exchange_pos.get("contracts", 0))
        if contracts <= 0:
            logger.info(f"HTF campaign SL vanished, position closed: {c.pair}")
            self._calculate_pnl(c, c.current_sl_price)
            self._close_campaign("trailing_sl")
        else:
            logger.warning(f"HTF campaign SL vanished, re-placing: {c.pair}")
            close_side = "sell" if c.direction == "long" else "buy"
            new_sl = await self._executor.place_stop_market(
                c.pair, close_side, c.total_size, c.current_sl_price
            )
            if new_sl:
                c.sl_order_id = new_sl.get("id")
                c.sl_fetch_failures = 0
            else:
                logger.error(f"HTF campaign: cannot re-place SL: {c.pair}")

    # ================================================================
    # Close campaign
    # ================================================================

    async def _close_all_and_market_close(self, c: PositionCampaign) -> None:
        """Cancel all orders and market close the position."""
        if c.sl_order_id:
            await self._executor.cancel_order(c.sl_order_id, c.pair)
        if c.pending_add and c.pending_add.order_id:
            await self._executor.cancel_order(c.pending_add.order_id, c.pair)

        close_side = "sell" if c.direction == "long" else "buy"
        if c.total_size > 0:
            await self._executor.close_position_market(c.pair, close_side, c.total_size)

        self._close_campaign("timeout")

    def _close_campaign(self, reason: str) -> None:
        """Transition campaign to closed and notify."""
        c = self._campaign
        if c is None:
            return

        c.phase = "closed"
        c.close_reason = reason
        c.closed_at = int(time.time())

        logger.info(
            f"HTF campaign CLOSED: {c.pair} {c.direction} reason={reason} "
            f"pnl={c.pnl_pct*100:.2f}% adds={len(c.adds)} "
            f"campaign_id={c.campaign_id}"
        )

        # Telegram notification (skip cancelled entries)
        if self._alert_manager is not None and reason != "cancelled":
            self._safe_notify(
                self._alert_manager.notify_campaign_closed(c)
            )

        self._persist_campaign_close(c)
        self._update_campaign_cache()

        # Notify risk service
        if self._risk is not None:
            if reason == "cancelled":
                self._risk.on_trade_cancelled(c.pair, c.direction)
            else:
                self._risk.on_trade_closed(
                    c.pair, c.direction, c.pnl_pct, c.closed_at
                )

    # ================================================================
    # Helpers
    # ================================================================

    def _calculate_pnl(self, c: PositionCampaign, exit_price: float) -> None:
        """Calculate PnL for the entire campaign."""
        if not c.weighted_entry or c.weighted_entry == 0 or c.total_size == 0:
            return

        if c.direction == "long":
            c.pnl_usd = (exit_price - c.weighted_entry) * c.total_size
        else:
            c.pnl_usd = (c.weighted_entry - exit_price) * c.total_size

        notional = c.weighted_entry * c.total_size
        c.pnl_pct = c.pnl_usd / notional if notional > 0 else 0.0

    def _safe_notify(self, coro) -> None:
        """Fire-and-forget notification."""
        task = asyncio.create_task(coro)
        task.add_done_callback(self._notify_done)

    @staticmethod
    def _notify_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Campaign notification failed: {exc}")

    # ================================================================
    # Persistence
    # ================================================================

    def _persist_campaign_open(self, c: PositionCampaign) -> None:
        """Insert campaign into PostgreSQL."""
        if self._data_store is None:
            return
        try:
            campaign_id = self._data_store.postgres.insert_campaign(c)
            c.db_campaign_id = campaign_id
        except Exception as e:
            logger.error(f"Failed to persist campaign open: {c.pair} {e}")

    def _persist_campaign_close(self, c: PositionCampaign) -> None:
        """Update campaign in PostgreSQL on close."""
        if self._data_store is None or c.db_campaign_id is None:
            return
        try:
            self._data_store.postgres.update_campaign(c)
        except Exception as e:
            logger.error(f"Failed to persist campaign close: {c.pair} {e}")

    def _update_campaign_cache(self) -> None:
        """Write current campaign state to Redis for dashboard."""
        if self._data_store is None:
            return
        try:
            c = self._campaign
            if c is None or c.phase == "closed":
                self._data_store.redis.set_bot_state("htf_campaign", "", ttl=600)
                return

            data = {
                "campaign_id": c.campaign_id,
                "pair": c.pair,
                "direction": c.direction,
                "phase": c.phase,
                "initial_entry_price": c.initial_entry_price,
                "actual_initial_entry": c.actual_initial_entry,
                "weighted_entry": c.weighted_entry,
                "current_sl_price": c.current_sl_price,
                "total_size": c.total_size,
                "total_margin": c.total_margin,
                "adds_count": len(c.adds),
                "ai_confidence": c.ai_confidence,
                "created_at": c.created_at,
                "filled_at": c.filled_at,
            }
            self._data_store.redis.set_bot_state("htf_campaign", json.dumps(data), ttl=600)
        except Exception as e:
            logger.error(f"Failed to update campaign cache: {e}")
