"""Main menu handler and callback router."""

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import keyboards, formatters
from telegram_bot.data_bridge import DataBridge


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


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all callback queries to the appropriate handler."""
    query = update.callback_query
    if not _is_authorized(update, context):
        await query.answer("Not authorized", show_alert=True)
        return

    await query.answer()
    data = query.data
    bridge = _get_bridge(context)

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
