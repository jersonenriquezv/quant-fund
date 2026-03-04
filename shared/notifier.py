"""
Telegram push notifications for key bot events.

Fire-and-forget — if Telegram fails, the bot continues normally.
Disabled gracefully if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.

Usage:
    from shared.notifier import TelegramNotifier
    notifier = TelegramNotifier()
    await notifier.notify_setup_detected(setup)
"""

import httpx

from shared.logger import setup_logger

logger = setup_logger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Sends Telegram messages on key bot events."""

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(token and chat_id)
        if not self._enabled:
            logger.info("Telegram notifications disabled (token or chat_id not set)")
        else:
            logger.info("Telegram notifications enabled")

    async def send(self, message: str) -> None:
        """Send a message to Telegram. Fire-and-forget."""
        if not self._enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    TELEGRAM_API.format(token=self._token),
                    json={
                        "chat_id": self._chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code != 200:
                    logger.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")

    async def notify_setup_detected(self, setup) -> None:
        """Setup found by Strategy Service."""
        msg = (
            f"\U0001f4ca <b>SETUP DETECTED</b>\n"
            f"{setup.pair} {setup.direction.upper()} ({setup.setup_type})\n"
            f"Entry: ${setup.entry_price:,.2f} | SL: ${setup.sl_price:,.2f}\n"
            f"Confluences: {', '.join(setup.confluences)}"
        )
        await self.send(msg)

    async def notify_ai_decision(self, setup, decision) -> None:
        """AI approved or rejected a setup."""
        pct = int(decision.confidence * 100)
        if decision.approved:
            msg = (
                f"\U0001f916 <b>AI APPROVED</b> \u2705 ({pct}%)\n"
                f"{setup.pair} {setup.direction.upper()}\n"
                f"\"{decision.reasoning}\""
            )
        else:
            msg = (
                f"\U0001f916 <b>AI REJECTED</b> \u274c ({pct}%)\n"
                f"{setup.pair} {setup.direction.upper()}\n"
                f"\"{decision.reasoning}\""
            )
        await self.send(msg)

    async def notify_risk_rejected(self, setup, reason: str) -> None:
        """Risk guardrail rejected a trade."""
        msg = (
            f"\U0001f6ab <b>RISK REJECTED</b>\n"
            f"{setup.pair} {setup.direction.upper()}\n"
            f"Reason: {reason}"
        )
        await self.send(msg)

    async def notify_trade_opened(self, pos) -> None:
        """Entry order filled — position is now active."""
        slippage = ""
        if pos.actual_entry_price and pos.entry_price > 0:
            slip_pct = abs(pos.actual_entry_price - pos.entry_price) / pos.entry_price * 100
            slippage = f" (slippage: {slip_pct:.3f}%)"
        msg = (
            f"\u2705 <b>TRADE OPENED</b>\n"
            f"{pos.pair} {pos.direction.upper()}\n"
            f"Entry: ${pos.actual_entry_price:,.2f}{slippage}\n"
            f"Size: {pos.filled_size:.6f} | Leverage: {int(pos.leverage)}x\n"
            f"SL: ${pos.sl_price:,.2f} | TP1: ${pos.tp1_price:,.2f}"
        )
        await self.send(msg)

    async def notify_trade_closed(self, pos) -> None:
        """Position closed — SL, TP, timeout, etc."""
        reason_label = (pos.close_reason or "unknown").upper()
        pnl_emoji = "\U0001f4b0" if pos.pnl_pct >= 0 else "\U0001f534"
        msg = (
            f"{pnl_emoji} <b>TRADE CLOSED — {reason_label}</b>\n"
            f"{pos.pair} {pos.direction.upper()}\n"
            f"P&amp;L: {pos.pnl_pct*100:+.2f}%"
        )
        await self.send(msg)

    _WHALE_ACTION_MAP = {
        "exchange_deposit": ("deposited to", "\U0001f534 BEARISH"),
        "exchange_withdrawal": ("withdrew from", "\U0001f7e2 BULLISH"),
        "transfer_out": ("transferred out to", "\U0001f7e1 NEUTRAL"),
        "transfer_in": ("received from", "\U0001f7e1 NEUTRAL"),
    }

    async def notify_whale_movement(self, movement) -> None:
        """Large whale transfer detected (ETH or BTC)."""
        action, signal = self._WHALE_ACTION_MAP.get(
            movement.action, (movement.action, "\u2753 UNKNOWN")
        )
        emoji = "\U0001f433"  # whale
        decimals = 4 if movement.chain == "BTC" else 2
        msg = (
            f"{emoji} <b>WHALE MOVEMENT</b>\n"
            f"{movement.amount:.{decimals}f} {movement.chain} {action} {movement.exchange}\n"
            f"Signal: {signal}\n"
            f"Significance: {movement.significance}"
        )
        await self.send(msg)

    async def notify_emergency(self, pos, reason: str) -> None:
        """Critical event — SL placement failure, emergency close."""
        msg = (
            f"\U0001f6a8 <b>EMERGENCY CLOSE</b>\n"
            f"{pos.pair} {pos.direction.upper()}\n"
            f"{reason}"
        )
        await self.send(msg)
