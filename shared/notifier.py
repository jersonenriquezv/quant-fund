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

    async def notify_ai_pre_filtered(self, setup, reason: str) -> None:
        """AI pre-filter rejected a setup before calling Claude."""
        msg = (
            f"\U0001f916 <b>AI PRE-FILTERED</b> \u26d4\n"
            f"{setup.pair} {setup.direction.upper()}\n"
            f"Reason: {reason}"
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
        wallet_line = ""
        if movement.wallet_label:
            wallet_line = f"Wallet: <b>{movement.wallet_label}</b>\n"
        else:
            truncated = movement.wallet[:6] + "..." + movement.wallet[-4:]
            wallet_line = f"Wallet: {truncated}\n"
        msg = (
            f"{emoji} <b>WHALE MOVEMENT</b>\n"
            f"{wallet_line}"
            f"{movement.amount:.{decimals}f} {movement.chain} {action} {movement.exchange}\n"
            f"Signal: {signal}\n"
            f"Significance: {movement.significance}"
        )
        await self.send(msg)

    async def notify_ob_summary(self, pair: str, obs: list, htf_bias: str) -> None:
        """Summary of active Order Blocks when 4H candle closes."""
        if not obs:
            msg = (
                f"\U0001f4e6 <b>4H OB SUMMARY — {pair}</b>\n"
                f"HTF Bias: {htf_bias.upper()}\n"
                f"No active Order Blocks"
            )
            await self.send(msg)
            return

        lines = []
        for ob in obs:
            arrow = "\U0001f7e2" if ob.direction == "bullish" else "\U0001f534"
            lines.append(
                f"  {arrow} {ob.direction.upper()} {ob.timeframe} "
                f"| {ob.low:.2f}-{ob.high:.2f} "
                f"| Entry: {ob.entry_price:.2f} "
                f"| Vol: {ob.volume_ratio:.1f}x"
            )
        ob_text = "\n".join(lines)
        msg = (
            f"\U0001f4e6 <b>4H OB SUMMARY — {pair}</b>\n"
            f"HTF Bias: {htf_bias.upper()}\n"
            f"Active OBs ({len(obs)}):\n"
            f"{ob_text}"
        )
        await self.send(msg)

    async def notify_hourly_status(
        self,
        uptime_str: str,
        profile: str,
        open_positions: int,
        trades_today: int,
        daily_dd_pct: float,
        weekly_dd_pct: float,
        prices: dict[str, float],
        htf_bias: dict[str, str],
    ) -> None:
        """Hourly bot status summary."""
        # Prices
        price_lines = []
        for pair, price in prices.items():
            short_pair = pair.replace("/USDT", "")
            bias = htf_bias.get(pair, "?")
            bias_icon = "\U0001f7e2" if bias == "bullish" else ("\U0001f534" if bias == "bearish" else "\u26aa")
            price_lines.append(f"  {short_pair}: ${price:,.2f} {bias_icon} {bias}")
        prices_text = "\n".join(price_lines) if price_lines else "  N/A"

        dd_daily_str = f"{daily_dd_pct*100:.1f}%"
        dd_weekly_str = f"{weekly_dd_pct*100:.1f}%"

        msg = (
            f"\U0001f4ca <b>HOURLY STATUS</b>\n"
            f"Uptime: {uptime_str}\n"
            f"Profile: <b>{profile}</b>\n"
            f"\n"
            f"Prices:\n{prices_text}\n"
            f"\n"
            f"Positions: {open_positions} open\n"
            f"Trades today: {trades_today}\n"
            f"DD daily: {dd_daily_str} | weekly: {dd_weekly_str}"
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
