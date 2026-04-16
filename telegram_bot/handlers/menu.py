"""Main menu handler and callback router."""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config.settings import settings
from shared.logger import setup_logger
from telegram_bot import keyboards, formatters
from telegram_bot.data_bridge import DataBridge

logger = setup_logger("telegram_bot")


def _get_bridge(context: ContextTypes.DEFAULT_TYPE) -> DataBridge:
    return context.bot_data["bridge"]


def _is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    return chat_id in context.bot_data["allowed_chat_ids"]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — show main menu."""
    if not _is_authorized(update, context):
        return
    await update.message.reply_text(
        "\U0001f916 <b>Quant Fund Bot</b>\n\nSelect a section:",
        parse_mode="HTML",
        reply_markup=keyboards.main_menu(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /menu — show main menu."""
    if not _is_authorized(update, context):
        return
    await update.message.reply_text(
        "\U0001f916 <b>Quant Fund Bot</b>\n\nSelect a section:",
        parse_mode="HTML",
        reply_markup=keyboards.main_menu(),
    )


async def emergency_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /emergency — 2-step confirm before closing all positions."""
    if not _is_authorized(update, context):
        return
    bridge = _get_bridge(context)
    positions = bridge.get_positions()
    count = len(positions) if positions else 0
    text = (
        "\u26a0\ufe0f <b>EMERGENCY HALT</b>\n\n"
        f"This will:\n"
        f"1. Halt all new trade execution\n"
        f"2. Cancel all pending entries\n"
        f"3. Market-close {count} active position(s)\n\n"
        f"<b>Are you sure?</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2705 CONFIRM", callback_data="emergency:confirm"),
            InlineKeyboardButton("\u274c Cancel", callback_data="emergency:cancel"),
        ]
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all callback queries to the appropriate handler."""
    query = update.callback_query
    if not _is_authorized(update, context):
        await query.answer("Not authorized", show_alert=True)
        return

    await query.answer()
    data = query.data
    bridge = _get_bridge(context)

    if data == "emergency:confirm":
        await _execute_emergency(query, bridge)
        return
    if data == "emergency:cancel":
        await query.edit_message_text("Emergency cancelled.")
        return

    if data == "back:menu":
        await query.edit_message_text(
            "\U0001f916 <b>Quant Fund Bot</b>\n\nSelect a section:",
            parse_mode="HTML",
            reply_markup=keyboards.main_menu(),
        )
        return

    if data == "menu:portfolio":
        await _show_portfolio(query, bridge)
    elif data == "menu:market":
        await _show_market(query, bridge)
    elif data == "menu:positions":
        await _show_positions(query, bridge)
    elif data == "menu:status":
        await _show_status(query, bridge)
    elif data == "menu:trades" or data == "trades:page:0":
        await _show_trades(query, bridge, page=0)
    elif data.startswith("trades:page:"):
        page = int(data.split(":")[2])
        await _show_trades(query, bridge, page=page)
    elif data == "menu:obs":
        await query.edit_message_text(
            "\U0001f4e6 <b>OBs Activos</b>\n\nSelect pair:",
            parse_mode="HTML",
            reply_markup=keyboards.obs_pair_selector(),
        )
    elif data.startswith("obs:"):
        pair_filter = data[4:]
        if pair_filter != "all" and "/" not in pair_filter:
            pair_filter = f"{pair_filter}/USDT"
        await _show_obs(query, bridge, pair_filter)


async def _show_portfolio(query, bridge: DataBridge) -> None:
    data = bridge.get_portfolio()
    text = formatters.format_portfolio(data)
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=keyboards.back_and_refresh("portfolio"),
    )


async def _show_market(query, bridge: DataBridge) -> None:
    data = bridge.get_market()
    text = formatters.format_market(data)
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=keyboards.back_and_refresh("market"),
    )


async def _show_positions(query, bridge: DataBridge) -> None:
    positions = bridge.get_positions()
    text = formatters.format_positions(positions)
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=keyboards.back_and_refresh("positions"),
    )


async def _show_status(query, bridge: DataBridge) -> None:
    data = bridge.get_bot_status()
    text = formatters.format_bot_status(data)
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=keyboards.back_and_refresh("status"),
    )


async def _show_trades(query, bridge: DataBridge, page: int = 0) -> None:
    trades, has_next = bridge.get_recent_trades(page=page)
    text = formatters.format_trades(trades, page=page)
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=keyboards.trades_pagination(page, has_next),
    )


async def _show_obs(query, bridge: DataBridge, pair_filter: str) -> None:
    obs = bridge.get_active_obs(pair_filter)
    text = formatters.format_obs(obs, pair_filter)
    await query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=keyboards.back_and_refresh("obs"),
    )


async def _execute_emergency(query, bridge: DataBridge) -> None:
    """Execute emergency halt: freeze execution + close all positions."""
    lines = ["\u26a0\ufe0f <b>EMERGENCY EXECUTING</b>\n"]

    # Step 1: Halt new trades
    settings.TRADING_HALTED = True
    lines.append("\u2705 Trading halted (TRADING_HALTED=true)")

    # Step 2: Close all positions
    es = bridge._es
    if es is not None:
        try:
            results = await es.close_all_positions()
            if results:
                for pair, status in results.items():
                    lines.append(f"  {pair}: {status}")
            else:
                lines.append("  No active positions to close")
        except Exception as e:
            lines.append(f"\u274c close_all_positions error: {e}")
            logger.error(f"Emergency close_all_positions failed: {e}")
    else:
        lines.append("  Execution service not available")

    lines.append(
        "\n<b>To resume:</b> set TRADING_HALTED=false in .env and restart, "
        "or restart the bot (flag is not persisted)."
    )

    await query.edit_message_text("\n".join(lines), parse_mode="HTML")
