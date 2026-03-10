"""
Intelligent alert routing with priorities, rate limiting, silencing, and batching.

Wraps TelegramNotifier with:
- Priority-based routing (INFO/WARNING/CRITICAL/EMERGENCY)
- Per-category auto-silencing (3 alerts in 5 min → 15 min silence)
- Sliding window rate limiting per priority
- Whale movement batching (2 min digest window)
- EMERGENCY escalation with retry + backoff

Usage:
    from shared.alert_manager import AlertManager, AlertPriority
    alert_mgr = AlertManager(notifier)
    await alert_mgr.alert(AlertPriority.CRITICAL, "trade_lifecycle", msg)
"""

import asyncio
import time
from enum import Enum

from config.settings import settings
from shared.logger import setup_logger
from shared.notifier import TelegramNotifier

logger = setup_logger("alert_manager")


class AlertPriority(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


# Categories that can NEVER be silenced
_UNSILENCEABLE = {"trade_lifecycle", "emergency"}


class AlertManager:
    """Routes alerts through silencing, rate limiting, batching, and escalation."""

    def __init__(self, notifier: TelegramNotifier) -> None:
        self._notifier = notifier

        # Silencing: category -> silence_until (unix timestamp)
        self._silenced: dict[str, float] = {}

        # Auto-silence tracking: category -> [timestamp, ...]
        self._auto_silence_history: dict[str, list[float]] = {}

        # Rate limiting: priority.value -> [timestamp, ...]
        self._rate_history: dict[str, list[float]] = {}

        # Whale batching
        self._whale_buffer: list[str] = []
        self._whale_batch_task: asyncio.Task | None = None
        self._whale_batch_lock = asyncio.Lock()

        # Stats
        self._suppressed_count: int = 0

    # ================================================================
    # Core routing
    # ================================================================

    async def alert(
        self, priority: AlertPriority, category: str, message: str
    ) -> bool:
        """Route an alert through silencing, rate limiting, and batching.

        Returns True if the message was sent (or buffered for batch).
        """
        now = time.time()

        # EMERGENCY: never silenced, never rate limited — escalation with retry
        if priority == AlertPriority.EMERGENCY:
            return await self._send_with_escalation(message)

        # Silencing check (skip for unsilenceable categories)
        if category not in _UNSILENCEABLE:
            if self._is_silenced(category, now):
                logger.debug(f"Alert silenced: {category}")
                self._suppressed_count += 1
                return False

            # Record for auto-silence
            self._record_auto_silence(category, now)

        # Rate limiting
        if not self._check_rate_limit(priority, now):
            logger.info(f"Alert rate limited: {priority.value} {category}")
            self._suppressed_count += 1
            return False

        # Whale batching — buffer low/medium significance
        if category == "whale_movement":
            await self._add_to_whale_batch(message)
            return True

        # Send directly
        success = await self._notifier.send(message)

        # CRITICAL gets 1 retry on failure
        if not success and priority == AlertPriority.CRITICAL:
            await asyncio.sleep(5)
            success = await self._notifier.send(message)

        return success

    # ================================================================
    # Silencing
    # ================================================================

    def silence(self, category: str, duration_seconds: int) -> bool:
        """Manually silence a category. Returns False if category is unsilenceable."""
        if category in _UNSILENCEABLE:
            return False
        self._silenced[category] = time.time() + duration_seconds
        logger.info(f"Category silenced: {category} for {duration_seconds}s")
        return True

    def unsilence(self, category: str) -> None:
        """Remove silence for a category."""
        self._silenced.pop(category, None)
        logger.info(f"Category unsilenced: {category}")

    def _is_silenced(self, category: str, now: float) -> bool:
        """Check if category is currently silenced."""
        until = self._silenced.get(category)
        if until is None:
            return False
        if now >= until:
            # Silence expired
            del self._silenced[category]
            return False
        return True

    def _record_auto_silence(self, category: str, now: float) -> None:
        """Track alerts per category and auto-silence if threshold exceeded."""
        history = self._auto_silence_history.setdefault(category, [])

        # Prune old entries outside the window
        window_start = now - settings.ALERT_AUTO_SILENCE_WINDOW
        history[:] = [t for t in history if t > window_start]

        history.append(now)

        if len(history) >= settings.ALERT_AUTO_SILENCE_THRESHOLD:
            self._silenced[category] = now + settings.ALERT_AUTO_SILENCE_DURATION
            logger.info(
                f"Auto-silenced: {category} ({len(history)} alerts in "
                f"{settings.ALERT_AUTO_SILENCE_WINDOW}s) — "
                f"silenced for {settings.ALERT_AUTO_SILENCE_DURATION}s"
            )

    # ================================================================
    # Rate limiting (sliding window per priority)
    # ================================================================

    def _check_rate_limit(self, priority: AlertPriority, now: float) -> bool:
        """Returns True if alert is within rate limit."""
        limit, window = self._get_rate_config(priority)
        if limit <= 0:
            return True  # No limit (EMERGENCY)

        key = priority.value
        history = self._rate_history.setdefault(key, [])

        # Prune old entries
        cutoff = now - window
        history[:] = [t for t in history if t > cutoff]

        if len(history) >= limit:
            return False

        history.append(now)
        return True

    @staticmethod
    def _get_rate_config(priority: AlertPriority) -> tuple[int, int]:
        """Return (max_count, window_seconds) for a priority level."""
        if priority == AlertPriority.INFO:
            return settings.ALERT_RATE_LIMIT_INFO, settings.ALERT_RATE_WINDOW_INFO
        elif priority == AlertPriority.WARNING:
            return settings.ALERT_RATE_LIMIT_WARNING, settings.ALERT_RATE_WINDOW_WARNING
        elif priority == AlertPriority.CRITICAL:
            return settings.ALERT_RATE_LIMIT_CRITICAL, settings.ALERT_RATE_WINDOW_CRITICAL
        else:
            return 0, 0  # EMERGENCY — no limit

    # ================================================================
    # Whale batching
    # ================================================================

    async def _add_to_whale_batch(self, message: str) -> None:
        """Buffer a whale movement message. Flush after batch window."""
        async with self._whale_batch_lock:
            self._whale_buffer.append(message)

            # Start flush timer if not already running
            if self._whale_batch_task is None or self._whale_batch_task.done():
                self._whale_batch_task = asyncio.create_task(
                    self._flush_whale_batch()
                )

    async def _flush_whale_batch(self) -> None:
        """Wait for batch window then send digest."""
        await asyncio.sleep(settings.ALERT_WHALE_BATCH_WINDOW)

        async with self._whale_batch_lock:
            messages = self._whale_buffer.copy()
            self._whale_buffer.clear()

        if not messages:
            return

        if len(messages) == 1:
            await self._notifier.send(messages[0])
            return

        # Build digest
        digest = (
            f"\U0001f40b <b>WHALE DIGEST</b> "
            f"({len(messages)} movements in {settings.ALERT_WHALE_BATCH_WINDOW // 60}min)\n\n"
        )
        # Truncate if too many to keep message readable
        shown = messages[:8]
        for msg in shown:
            # Strip the HTML bold header from individual messages for digest
            digest += f"{msg}\n\n"
        if len(messages) > 8:
            digest += f"... +{len(messages) - 8} more"

        await self._notifier.send(digest)

    async def send_whale_immediate(self, message: str) -> bool:
        """Send a high-significance whale alert immediately (bypass batch)."""
        return await self._notifier.send(message)

    # ================================================================
    # EMERGENCY escalation with retry
    # ================================================================

    async def _send_with_escalation(self, message: str) -> bool:
        """Send with retry + backoff for EMERGENCY alerts."""
        delays = [0, 5, 15, 30]
        for attempt, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(delay)
            success = await self._notifier.send(message)
            if success:
                return True
            logger.warning(
                f"EMERGENCY delivery attempt {attempt + 1}/{len(delays)} failed"
            )

        logger.critical(f"EMERGENCY UNDELIVERABLE after {len(delays)} attempts: {message}")
        return False

    # ================================================================
    # Convenience methods — format + route (drop-in for TelegramNotifier)
    # ================================================================

    async def notify_trade_opened(self, pos) -> None:
        """Trade opened — CRITICAL priority."""
        slippage = ""
        if pos.actual_entry_price and pos.entry_price > 0:
            slip_pct = abs(pos.actual_entry_price - pos.entry_price) / pos.entry_price * 100
            slippage = f" (slippage: {slip_pct:.3f}%)"
        msg = (
            f"\u2705 <b>TRADE OPENED</b>\n"
            f"{pos.pair} {pos.direction.upper()}\n"
            f"Entry: ${pos.actual_entry_price:,.2f}{slippage}\n"
            f"Size: {pos.filled_size:.6f} | Leverage: {int(pos.leverage)}x\n"
            f"SL: ${pos.sl_price:,.2f} | TP: ${pos.tp2_price:,.2f}"
        )
        await self.alert(AlertPriority.CRITICAL, "trade_lifecycle", msg)

    async def notify_trade_closed(self, pos) -> None:
        """Trade closed — CRITICAL priority."""
        reason_label = (pos.close_reason or "unknown").upper()
        pnl_emoji = "\U0001f4b0" if pos.pnl_pct >= 0 else "\U0001f534"
        msg = (
            f"{pnl_emoji} <b>TRADE CLOSED — {reason_label}</b>\n"
            f"{pos.pair} {pos.direction.upper()}\n"
            f"P&amp;L: {pos.pnl_pct*100:+.2f}%"
        )
        await self.alert(AlertPriority.CRITICAL, "trade_lifecycle", msg)

    async def notify_emergency(self, pos, reason: str) -> None:
        """Emergency event — EMERGENCY priority with retry."""
        msg = (
            f"\U0001f6a8 <b>EMERGENCY CLOSE</b>\n"
            f"{pos.pair} {pos.direction.upper()}\n"
            f"{reason}"
        )
        await self.alert(AlertPriority.EMERGENCY, "emergency", msg)

    async def notify_ai_decision(self, setup, decision) -> None:
        """AI decision — WARNING if approved, INFO if rejected."""
        pct = int(decision.confidence * 100)
        if decision.approved:
            msg = (
                f"\U0001f916 <b>AI APPROVED</b> \u2705 ({pct}%)\n"
                f"{setup.pair} {setup.direction.upper()}\n"
                f"\"{decision.reasoning}\""
            )
            await self.alert(AlertPriority.WARNING, "ai_decision", msg)
        else:
            msg = (
                f"\U0001f916 <b>AI REJECTED</b> \u274c ({pct}%)\n"
                f"{setup.pair} {setup.direction.upper()}\n"
                f"\"{decision.reasoning}\""
            )
            await self.alert(AlertPriority.INFO, "ai_decision", msg)

    async def notify_whale_movement(self, movement, immediate: bool = False) -> None:
        """Whale movement — INFO priority, batched unless immediate.

        Only exchange deposits/withdrawals reach here (filtered by data_service).
        Format includes directional signal: BEARISH (deposit) / BULLISH (withdrawal).
        """
        decimals = 4 if movement.chain == "BTC" else 2
        label = movement.wallet_label or (movement.wallet[:6] + "..." + movement.wallet[-4:])
        amount_str = f"{movement.amount:,.{decimals}f} {movement.chain}"
        usd_val = getattr(movement, "amount_usd", 0)
        if usd_val > 0:
            amount_str += f" ({TelegramNotifier._format_usd(usd_val)})"

        if movement.action == "exchange_deposit":
            signal_emoji = "\U0001f534"  # Red circle
            signal_text = "BEARISH — selling pressure"
            direction = f" to {movement.exchange}"
        elif movement.action == "exchange_withdrawal":
            signal_emoji = "\U0001f7e2"  # Green circle
            signal_text = "BULLISH — accumulation"
            direction = f" from {movement.exchange}"
        else:
            signal_emoji = "\u26aa"  # White circle
            signal_text = "Neutral"
            direction = f" to {movement.exchange}" if movement.action == "transfer_out" else f" from {movement.exchange}"

        msg = (
            f"\U0001f40b {signal_emoji} <b>WHALE {signal_text.split(' — ')[0]}</b>\n"
            f"{label} → {amount_str}{direction}\n"
            f"{signal_text}"
        )

        if immediate:
            await self.send_whale_immediate(msg)
        else:
            await self.alert(AlertPriority.INFO, "whale_movement", msg)

    async def notify_ob_summary(
        self, pair: str, obs: list, htf_bias: str, current_price: float = 0.0,
    ) -> None:
        """OB summary — INFO priority. Delegates formatting to TelegramNotifier."""
        # Reuse TelegramNotifier's formatting (it's complex)
        await self._notifier.notify_ob_summary(pair, obs, htf_bias, current_price)

    async def notify_daily_summary(
        self,
        uptime_str: str,
        profile: str,
        trades_today: int,
        wins: int,
        losses: int,
        open_positions: int,
        daily_dd_pct: float,
        weekly_dd_pct: float,
        capital: float,
        prices: dict[str, float],
    ) -> None:
        """Daily summary at 00:00 UTC — INFO priority."""
        price_lines = []
        for pair, price in prices.items():
            short = pair.replace("/USDT", "")
            price_lines.append(f"  {short}: ${price:,.2f}")
        prices_text = "\n".join(price_lines) if price_lines else "  N/A"

        net_pnl = wins - losses  # crude indicator
        wl_str = f"{wins}W / {losses}L" if (wins + losses) > 0 else "no trades"

        msg = (
            f"\U0001f4ca <b>DAILY SUMMARY</b>\n"
            f"Uptime: {uptime_str} | Profile: <b>{profile}</b>\n"
            f"\n"
            f"Trades: {trades_today} ({wl_str})\n"
            f"Open positions: {open_positions}\n"
            f"Capital: ${capital:,.2f}\n"
            f"DD: daily {daily_dd_pct*100:.1f}% | weekly {weekly_dd_pct*100:.1f}%\n"
            f"\n"
            f"Prices:\n{prices_text}"
        )
        await self.alert(AlertPriority.INFO, "daily_summary", msg)

    async def notify_bot_started(self, mode: str, capital: float) -> None:
        """Bot started — CRITICAL priority (always want to know about restarts)."""
        msg = (
            f"\U0001f680 <b>BOT STARTED</b>\n"
            f"Mode: <b>{mode}</b>\n"
            f"Capital: ${capital:,.2f}\n"
            f"Profile: {settings.STRATEGY_PROFILE}"
        )
        await self.alert(AlertPriority.CRITICAL, "trade_lifecycle", msg)

    async def notify_breakeven_sl(self, pair: str, direction: str, entry_price: float) -> None:
        """SL moved to breakeven — INFO priority."""
        msg = (
            f"\U0001f6e1 <b>BREAKEVEN</b>\n"
            f"{pair} {direction.upper()} — SL moved to entry (${entry_price:,.2f})"
        )
        await self.alert(AlertPriority.INFO, "sl_management", msg)

    async def notify_trailing_sl(self, pair: str, direction: str, new_sl: float) -> None:
        """SL moved to TP1 (trailing) — INFO priority."""
        msg = (
            f"\U0001f4c8 <b>TRAILING SL</b>\n"
            f"{pair} {direction.upper()} — SL moved to ${new_sl:,.2f}"
        )
        await self.alert(AlertPriority.INFO, "sl_management", msg)

    async def notify_entry_expired(self, pair: str, direction: str, entry_price: float) -> None:
        """Entry order expired unfilled — INFO priority."""
        msg = (
            f"\u23f0 <b>ENTRY EXPIRED</b>\n"
            f"{pair} {direction.upper()} — limit at ${entry_price:,.2f} not filled"
        )
        await self.alert(AlertPriority.INFO, "trade_lifecycle", msg)

    async def notify_dd_warning(self, daily_dd_pct: float, limit_pct: float) -> None:
        """Drawdown approaching limit — WARNING priority."""
        msg = (
            f"\u26a0\ufe0f <b>DRAWDOWN WARNING</b>\n"
            f"Daily DD: {daily_dd_pct*100:.1f}% — approaching limit ({limit_pct*100:.0f}%)"
        )
        await self.alert(AlertPriority.WARNING, "risk_warning", msg)

    async def notify_health_down(self, components: list[str]) -> None:
        """Infrastructure component down — WARNING priority."""
        msg = (
            f"\u26a0\ufe0f <b>HEALTH CHECK — DOWN</b>\n"
            f"Components: {', '.join(components)}"
        )
        await self.alert(AlertPriority.WARNING, "health_check", msg)

    async def notify_health_recovered(self, components: list[str]) -> None:
        """Infrastructure component recovered — INFO priority."""
        msg = (
            f"\u2705 <b>HEALTH CHECK — RECOVERED</b>\n"
            f"Components: {', '.join(components)}"
        )
        await self.alert(AlertPriority.INFO, "health_check", msg)

    # ================================================================
    # Stats
    # ================================================================

    @property
    def suppressed_count(self) -> int:
        """Total alerts suppressed by silencing or rate limiting."""
        return self._suppressed_count
