"""HTML message formatters for Telegram bot sections."""

import time
from datetime import datetime, timezone


def format_usd(value: float) -> str:
    """Format USD value with K/M shorthand."""
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,.2f}"


def format_pnl(pct: float, usd: float = 0.0) -> str:
    """Format P&L with emoji and percentage."""
    emoji = "\U0001f7e2" if pct >= 0 else "\U0001f534"
    s = f"{emoji} {pct * 100:+.2f}%"
    if usd != 0:
        s += f" ({format_usd(usd)})"
    return s


def format_duration(seconds: float) -> str:
    """Format duration as Xd Xh Xm."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_timestamp(ts) -> str:
    """Format a datetime or unix timestamp to short string."""
    if ts is None:
        return "—"
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        dt = ts
    return dt.strftime("%m/%d %H:%M")


def format_portfolio(data: dict) -> str:
    """Format portfolio summary."""
    capital = data["capital"]
    daily_pnl = data["daily_pnl_pct"]
    weekly_pnl = data["weekly_pnl_pct"]
    trades_today = data["trades_today"]
    heat_usd = data["heat_usd"]
    positions = data["positions"]

    lines = [
        f"\U0001f4b0 <b>Portfolio</b>",
        f"Capital: <b>{format_usd(capital)}</b>",
        f"Daily P&L: {format_pnl(daily_pnl)}",
        f"Weekly P&L: {format_pnl(weekly_pnl)}",
        f"Trades today: {trades_today}",
        f"Heat: {format_usd(heat_usd)}",
    ]

    if positions:
        lines.append(f"\n<b>Open Positions ({len(positions)})</b>")
        for p in positions:
            pair_short = p["pair"].replace("/USDT", "")
            arrow = "\u2b06" if p["direction"] == "long" else "\u2b07"
            pnl_str = format_pnl(p["unrealized_pnl_pct"], p["unrealized_pnl_usd"])
            lines.append(
                f"  {arrow} <b>{pair_short}</b> {p['direction'].upper()} "
                f"@ ${p['entry']:,.2f} | {pnl_str}"
            )
    else:
        lines.append("\nNo open positions")

    return "\n".join(lines)


def format_market(data: dict) -> str:
    """Format market overview."""
    lines = ["\U0001f4c8 <b>Mercado</b>"]

    for pair_data in data["prices"]:
        pair_short = pair_data["pair"].replace("/USDT", "")
        price = pair_data["price"]
        change = pair_data.get("change_24h")
        change_str = f" ({change:+.1f}%)" if change is not None else ""
        lines.append(f"  <b>{pair_short}</b>: ${price:,.2f}{change_str}")

    if data.get("funding"):
        lines.append("\n<b>Funding Rates</b>")
        for fr in data["funding"]:
            pair_short = fr["pair"].replace("/USDT", "")
            rate = fr["rate"]
            emoji = "\U0001f7e2" if rate < 0 else ("\U0001f534" if rate > 0.0001 else "\u26aa")
            lines.append(f"  {emoji} {pair_short}: {rate * 100:.4f}%")

    if data.get("fear_greed"):
        fg = data["fear_greed"]
        lines.append(f"\nFear & Greed: <b>{fg['score']}</b> ({fg['label']})")

    return "\n".join(lines)


def format_positions(positions: list[dict]) -> str:
    """Format detailed positions view."""
    if not positions:
        return "\U0001f4cb <b>Posiciones</b>\n\nNo open positions"

    lines = [f"\U0001f4cb <b>Posiciones ({len(positions)})</b>"]

    for p in positions:
        pair_short = p["pair"].replace("/USDT", "")
        arrow = "\u2b06" if p["direction"] == "long" else "\u2b07"
        pnl_str = format_pnl(p["unrealized_pnl_pct"], p["unrealized_pnl_usd"])
        duration = format_duration(time.time() - p["filled_at"]) if p.get("filled_at") else "—"

        lines.append(
            f"\n{arrow} <b>{pair_short}</b> {p['direction'].upper()} | {pnl_str}"
        )
        lines.append(f"  Entry: ${p['entry']:,.2f} | Lev: {int(p['leverage'])}x")
        lines.append(f"  SL: ${p['sl']:,.2f} | TP: ${p['tp']:,.2f}")
        lines.append(f"  Size: {p['size']:.6f} | Time: {duration}")
        if p.get("phase"):
            lines.append(f"  Phase: {p['phase']}")

    return "\n".join(lines)


def format_bot_status(data: dict) -> str:
    """Format bot status."""
    uptime = format_duration(data["uptime_seconds"])
    state = data.get("data_state", "unknown")

    lines = [
        f"\u2699\ufe0f <b>Bot Status</b>",
        f"Uptime: <b>{uptime}</b>",
        f"Data Service: {state}",
    ]

    if data.get("last_setup_time"):
        ago = format_duration(time.time() - data["last_setup_time"])
        lines.append(f"Last setup: {ago} ago")
    else:
        lines.append("Last setup: none")

    if data.get("shadow_count", 0) > 0:
        lines.append(f"\nShadow Mode: {data['shadow_count']} active")
        if data.get("shadow_filled", 0) > 0:
            lines.append(f"  Filled: {data['shadow_filled']}")

    if data.get("open_positions", 0) > 0:
        lines.append(f"Open positions: {data['open_positions']}")

    return "\n".join(lines)


def format_trades(trades: list[dict], page: int = 0) -> str:
    """Format recent closed trades."""
    if not trades:
        return "\U0001f4ca <b>Trades Recientes</b>\n\nNo closed trades"

    lines = [f"\U0001f4ca <b>Trades Recientes</b> (page {page + 1})"]

    for t in trades:
        pair_short = t["pair"].replace("/USDT", "")
        arrow = "\u2b06" if t["direction"] == "long" else "\u2b07"
        pnl_pct = t.get("pnl_pct") or 0
        pnl_usd = t.get("pnl_usd") or 0
        pnl_str = format_pnl(pnl_pct, pnl_usd)
        reason = (t.get("exit_reason") or "—").upper()
        closed = format_timestamp(t.get("closed_at"))

        lines.append(
            f"\n{arrow} <b>{pair_short}</b> {t['direction'].upper()} "
            f"({t.get('setup_type', '?')})"
        )
        lines.append(f"  {pnl_str} | {reason}")
        lines.append(f"  Closed: {closed}")

    return "\n".join(lines)


def format_obs(obs_data: list[dict], pair_filter: str | None = None) -> str:
    """Format active order blocks."""
    title = "\U0001f4e6 <b>OBs Activos</b>"
    if pair_filter and pair_filter != "all":
        title += f" — {pair_filter.replace('/USDT', '')}"

    if not obs_data:
        return f"{title}\n\nNo active order blocks"

    lines = [title]

    # Group by pair
    by_pair: dict[str, list] = {}
    for ob in obs_data:
        by_pair.setdefault(ob["pair"], []).append(ob)

    for pair, obs in by_pair.items():
        pair_short = pair.replace("/USDT", "")
        bias = obs[0].get("htf_bias", "undefined")
        bias_icon = "\U0001f7e2" if bias == "bullish" else (
            "\U0001f534" if bias == "bearish" else "\u26aa"
        )
        price = obs[0].get("current_price", 0)

        lines.append(f"\n<b>{pair_short}</b> {bias_icon} {bias.upper()} | ${price:,.2f}")

        for ob in obs[:5]:
            dist = ob.get("distance_pct", 0)
            tf = ob.get("timeframe", "?")
            vol = ob.get("volume_ratio", 0)
            direction = "\u2b06" if ob.get("direction") == "bullish" else "\u2b07"

            lines.append(
                f"  {direction} ${ob['entry_price']:,.2f} ({dist:+.2f}%) "
                f"| {tf} | vol {vol:.1f}x"
            )

        if len(obs) > 5:
            lines.append(f"  ... +{len(obs) - 5} more")

    return "\n".join(lines)
