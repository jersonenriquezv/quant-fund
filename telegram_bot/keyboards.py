"""Inline keyboard builders for the interactive Telegram bot."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import settings


def main_menu() -> InlineKeyboardMarkup:
    """Main menu — 2-column grid of section buttons."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f4b0 Portfolio", callback_data="menu:portfolio"),
            InlineKeyboardButton("\U0001f4c8 Mercado", callback_data="menu:market"),
        ],
        [
            InlineKeyboardButton("\U0001f4cb Posiciones", callback_data="menu:positions"),
            InlineKeyboardButton("\u2699\ufe0f Bot Status", callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton("\U0001f4ca Trades", callback_data="menu:trades"),
            InlineKeyboardButton("\U0001f4e6 OBs Activos", callback_data="menu:obs"),
        ],
    ])


def back_and_refresh(section: str) -> InlineKeyboardMarkup:
    """Standard footer: Refresh + Back to Menu."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"menu:{section}"),
            InlineKeyboardButton("\u25c0 Menu", callback_data="back:menu"),
        ],
    ])


def obs_pair_selector() -> InlineKeyboardMarkup:
    """Per-pair filter for OBs Activos."""
    pairs = settings.TRADING_PAIRS
    # Build rows of 4 buttons
    buttons = [
        InlineKeyboardButton(
            p.replace("/USDT", ""),
            callback_data=f"obs:{p}",
        )
        for p in pairs
    ]
    buttons.append(InlineKeyboardButton("ALL", callback_data="obs:all"))

    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("\u25c0 Menu", callback_data="back:menu")])
    return InlineKeyboardMarkup(rows)


def trades_pagination(page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Pagination for trades list."""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("\u25c0 Prev", callback_data=f"trades:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next \u25b6", callback_data=f"trades:page:{page + 1}"))

    rows = []
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("\U0001f504 Refresh", callback_data="trades:page:0"),
        InlineKeyboardButton("\u25c0 Menu", callback_data="back:menu"),
    ])
    return InlineKeyboardMarkup(rows)
