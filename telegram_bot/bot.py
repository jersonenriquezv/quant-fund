"""Interactive Telegram bot with inline keyboard navigation.

Runs alongside the existing TelegramNotifier (push alerts).
Uses python-telegram-bot's polling to receive commands and callback queries.
"""

from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from shared.logger import setup_logger
from telegram_bot.data_bridge import DataBridge
from telegram_bot.handlers.menu import start_command, menu_command, callback_router

logger = setup_logger("telegram_bot")


class TelegramInteractiveBot:
    """Interactive Telegram bot with inline keyboards for querying bot state."""

    def __init__(
        self,
        token: str,
        allowed_chat_ids: set[int],
        data_service,
        strategy_service,
        risk_service,
        execution_service,
        shadow_monitor,
        bot_start_time: float,
        get_last_setup_time=None,
    ) -> None:
        self._token = token
        self._allowed = allowed_chat_ids

        self._bridge = DataBridge(
            data_service=data_service,
            strategy_service=strategy_service,
            risk_service=risk_service,
            execution_service=execution_service,
            shadow_monitor=shadow_monitor,
            get_last_setup_time=get_last_setup_time or (lambda: 0.0),
            bot_start_time=bot_start_time,
        )

        self._app = Application.builder().token(token).build()

        # Store shared data for handlers
        self._app.bot_data["bridge"] = self._bridge
        self._app.bot_data["allowed_chat_ids"] = self._allowed

        # Register handlers
        self._app.add_handler(CommandHandler("start", start_command))
        self._app.add_handler(CommandHandler("menu", menu_command))
        self._app.add_handler(CallbackQueryHandler(callback_router))

        logger.info("Telegram interactive bot initialized")

    async def start(self) -> None:
        """Start the bot polling loop (integrates with existing event loop)."""
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram interactive bot started (polling)")

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        try:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            if self._app.running:
                await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram interactive bot stopped")
        except Exception as e:
            logger.warning(f"Telegram bot stop error: {e}")
