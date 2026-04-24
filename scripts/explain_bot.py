"""Telegram /explain bot — trading education on-demand.

Commands:
    /explain <concept>      — get explanation of a trading concept
    /review [days]          — trigger weekly review on demand
    /stats [days]           — quick stats from bybit_trade_annotations

Runs as long-lived polling daemon. Single-user (settings.TELEGRAM_CHAT_ID).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import psycopg2
from anthropic import AsyncAnthropic
from pybit.unified_trading import HTTP as BybitHTTP

from config.settings import settings
from shared.logger import setup_logger

logger = setup_logger("explain_bot")

TELEGRAM_BASE = "https://api.telegram.org/bot{token}"

EXPLAIN_SYSTEM = """You are a senior crypto derivatives coach. Explain trading concepts clearly and concisely for a retail trader learning smart money concepts (SMC), order flow, and risk management.

Rules:
- Max 400 words per response
- Use simple analogies, no jargon without definition
- Always end with a 1-line "How to use this in practice"
- Use markdown headings and bullet points for readability
- If the concept is a misconception, gently correct with the mechanics
- Focus on crypto perp futures context (Bybit/OKX UTA)
"""


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


class ExplainBot:
    def __init__(self) -> None:
        if not settings.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY missing")
        self._token = settings.TELEGRAM_BOT_TOKEN
        self._chat_id = str(settings.TELEGRAM_CHAT_ID)
        self._claude = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._offset: int | None = None
        self._bybit: BybitHTTP | None = None
        if settings.BYBIT_API_KEY and settings.BYBIT_API_SECRET:
            self._bybit = BybitHTTP(
                testnet=settings.BYBIT_TESTNET,
                api_key=settings.BYBIT_API_KEY,
                api_secret=settings.BYBIT_API_SECRET,
            )

    def _api(self, method: str) -> str:
        return f"{TELEGRAM_BASE.format(token=self._token)}/{method}"

    async def _send(self, client: httpx.AsyncClient, text: str, reply_to: int | None = None) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            await client.post(self._api("sendMessage"), json=payload, timeout=15)
        except Exception as exc:
            logger.error(f"send failed: {exc}")

    async def _explain(self, concept: str) -> str:
        if not concept:
            return "Usage: `/explain <concept>` — e.g. `/explain order block`, `/explain CVD divergence`"
        resp = await self._claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=EXPLAIN_SYSTEM,
            messages=[{"role": "user", "content": f"Explain: {concept}"}],
            temperature=0.4,
        )
        text = resp.content[0].text if resp.content else "(empty response)"
        return text

    def _balance(self) -> str:
        if not self._bybit:
            return "Bybit API not configured."
        try:
            resp = self._bybit.get_wallet_balance(accountType="UNIFIED")
        except Exception as exc:
            return f"balance fetch failed: {exc}"
        accts = (resp.get("result") or {}).get("list", []) or []
        if not accts:
            return "No UTA account found."
        a = accts[0]
        equity = float(a.get("totalEquity") or 0)
        margin_bal = float(a.get("totalMarginBalance") or 0)
        avail = float(a.get("totalAvailableBalance") or 0)
        upnl = float(a.get("totalPerpUPL") or 0)
        im = float(a.get("totalInitialMargin") or 0)
        mm = float(a.get("totalMaintenanceMargin") or 0)
        coins = a.get("coin") or []

        lines = [
            "*💰 Bybit UTA Balance*",
            f"Equity: `${equity:,.2f}`",
            f"Available: `${avail:,.2f}`",
            f"Margin balance: `${margin_bal:,.2f}`",
        ]
        if im > 0:
            lines.append(f"Initial margin: `${im:,.2f}`")
            lines.append(f"Maintenance margin: `${mm:,.2f}`")
        if upnl:
            sign = "✅" if upnl >= 0 else "❌"
            lines.append(f"Unrealized P&L: {sign} `${upnl:+,.2f}`")

        usdt_coin = next((c for c in coins if c.get("coin") == "USDT"), None)
        if usdt_coin:
            wb = float(usdt_coin.get("walletBalance") or 0)
            lines.append("")
            lines.append(f"USDT wallet: `{wb:,.4f}`")

        non_zero = [
            c for c in coins
            if c.get("coin") != "USDT" and float(c.get("walletBalance") or 0) > 0
        ]
        if non_zero:
            lines.append("")
            lines.append("*Other coins (>0):*")
            for c in non_zero[:8]:
                wb = float(c.get("walletBalance") or 0)
                usd = float(c.get("usdValue") or 0)
                lines.append(f"• {c.get('coin')}: `{wb:,.4f}` (≈ ${usd:,.2f})")
        return "\n".join(lines)

    def _stats(self, days: int) -> str:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status='closed') closed,
                    COUNT(*) FILTER (WHERE status='open') opened,
                    COALESCE(SUM(pnl_usd), 0) pnl,
                    COUNT(*) FILTER (WHERE pnl_usd > 0) wins,
                    COUNT(*) FILTER (WHERE pnl_usd < 0) losses,
                    COUNT(*) FILTER (WHERE thesis_pre IS NOT NULL) annotated
                FROM bybit_trade_annotations
                WHERE opened_at >= NOW() - (%s * INTERVAL '1 day')
                """,
                (days,),
            )
            r = cur.fetchone()
        if not r:
            return "No data."
        closed, opened, pnl, wins, losses, annotated = r
        total = (closed or 0) + (opened or 0)
        wr = (wins / closed * 100) if closed else 0
        sign = "✅" if pnl >= 0 else "❌"
        return (
            f"*Bybit stats — last {days}d*\n"
            f"Trades: {total} ({opened} open, {closed} closed)\n"
            f"Wins / Losses: {wins} / {losses}\n"
            f"Win rate: `{wr:.1f}%`\n"
            f"Net PnL: {sign} `${pnl:+.2f}`\n"
            f"Annotated: {annotated}/{total}"
        )

    async def _handle(self, client: httpx.AsyncClient, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        msg_id = msg.get("message_id")
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != self._chat_id:
            return  # single-user bot
        if not text.startswith("/"):
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/explain":
            await self._send(client, "Pensando…", reply_to=msg_id)
            try:
                reply = await self._explain(arg)
            except Exception as exc:
                reply = f"Error: {exc}"
            await self._send(client, reply, reply_to=msg_id)

        elif cmd == "/stats":
            days = int(arg) if arg.isdigit() else 7
            try:
                await self._send(client, self._stats(days), reply_to=msg_id)
            except Exception as exc:
                await self._send(client, f"stats error: {exc}", reply_to=msg_id)

        elif cmd == "/balance":
            try:
                await self._send(client, self._balance(), reply_to=msg_id)
            except Exception as exc:
                await self._send(client, f"balance error: {exc}", reply_to=msg_id)

        elif cmd == "/review":
            days = int(arg) if arg.isdigit() else 7
            await self._send(client, f"Generando review de {days}d… (~20s)", reply_to=msg_id)
            try:
                from scripts.weekly_review_bybit import (
                    build_user_prompt, generate_review, save_review, fetch_trades, ensure_reviews_table
                )
                ensure_reviews_table()
                trades = fetch_trades(days)
                if not trades:
                    await self._send(client, "No trades in period.", reply_to=msg_id)
                    return
                prompt = build_user_prompt(trades, days)
                report, tin, tout = await generate_review(prompt)
                save_review(days, trades, report, "claude-sonnet-4-6", tin, tout)
                # Telegram max 4096 chars; chunk if needed
                for chunk in self._chunk(report, 3800):
                    await self._send(client, chunk, reply_to=msg_id)
            except Exception as exc:
                await self._send(client, f"review error: {exc}", reply_to=msg_id)

        elif cmd == "/check":
            await self._send(client, "Checando setup…", reply_to=msg_id)
            try:
                from scripts.pretrade_check import parse_command, run_full, format_telegram
                parsed = parse_command(text)
                if parsed.error:
                    await self._send(client, parsed.error, reply_to=msg_id)
                    return
                model = settings.CLAUDE_MODEL_AUDIT
                report, payload, check_id = await run_full(parsed, model=model, bybit=self._bybit)
                reply = format_telegram(parsed, payload, report, check_id)
                for chunk in self._chunk(reply, 3800):
                    await self._send(client, chunk, reply_to=msg_id)
            except Exception as exc:
                logger.exception("check failed")
                await self._send(client, f"check error: {exc}", reply_to=msg_id)

        elif cmd == "/help":
            await self._send(
                client,
                "*Commands*\n"
                "`/balance` — Bybit UTA balance + positions\n"
                "`/stats [days]` — quick stats (default 7d)\n"
                "`/review [days]` — full Claude review (default 7d)\n"
                "`/check SYMBOL side entry SL TP [lev=N] [thesis…]` — pre-trade sanity check\n"
                "`/explain <concept>` — explanation",
                reply_to=msg_id,
            )

    @staticmethod
    def _chunk(text: str, size: int) -> list[str]:
        out: list[str] = []
        while text:
            out.append(text[:size])
            text = text[size:]
        return out

    async def run(self) -> None:
        logger.info(f"explain_bot: polling (chat_id={self._chat_id})")
        async with httpx.AsyncClient(timeout=35) as client:
            while True:
                try:
                    params = {"timeout": 30}
                    if self._offset is not None:
                        params["offset"] = self._offset
                    resp = await client.get(self._api("getUpdates"), params=params)
                    data = resp.json()
                    for upd in data.get("result", []):
                        self._offset = upd["update_id"] + 1
                        msg = upd.get("message")
                        if msg:
                            await self._handle(client, msg)
                except Exception as exc:
                    logger.error(f"poll loop error: {exc}")
                    await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(ExplainBot().run())
