"""Pre-trade Bybit checklist via Claude Opus 4.7.

Takes a proposed manual Bybit trade (symbol, side, entry, SL, TP, optional
thesis) and produces a structured second-opinion: score 0-10, verdict, red
flags, missing confluences, position size suggestion. Logs every request to
`bybit_pretrade_checks` for later regression analysis.

Called from `scripts/explain_bot.py` (`/check` command) or standalone CLI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor
from anthropic import AsyncAnthropic
from pybit.unified_trading import HTTP as BybitHTTP

from config.settings import settings
from shared.logger import setup_logger

logger = setup_logger("pretrade_check")


# Allowed symbols — aligned with bot's linear perps + BTC/ETH/SOL trio
ALLOWED_BASES = {"BTC", "ETH", "SOL", "DOGE", "XRP", "LINK", "AVAX"}


SYSTEM_PROMPT = """You are a senior discretionary crypto futures coach doing a final pre-trade sanity check on a manual Bybit perpetual trade before entry. You are NOT endorsing or rejecting the trade — you are a second set of eyes checking risk, confluences, and edge alignment against the trader's own recent history.

Core principles:
- Process > outcome. A winning trade with broken rules is still a process failure.
- Counter-HTF trades must earn the right with strong confluence; flag when missing.
- R:R below 1.5 needs exceptional context. Above 3.0 often means the TP is unrealistic.
- Leverage should match setup quality. x20+ only for A+ setups with tight, structural SLs.
- Risk per trade: ~2% of account. Position size implied by entry/SL distance must not exceed this.
- If thesis is vague ("feels good", "pumping", "whales buying"), call it out — no actionable edge.
- If proposal contradicts what the trader's own recent similar trades show (e.g. their last 5 BTC longs at 18–20 UTC are 0/5), say so.

Output format (strict JSON, no markdown fences):
{
  "score": 0-10 integer (10 = A+ setup, 0 = do not take),
  "verdict": one of "strong", "ok", "weak", "skip",
  "risk_pct_of_account": float (implied risk / balance * 100),
  "rr_ratio": float,
  "size_suggestion_usd": float (notional to risk ~2% given entry/SL distance and balance),
  "leverage_suggestion": int (max safe leverage given margin),
  "green_flags": [short strings — confluences present],
  "red_flags": [short strings — specific issues],
  "missing": [short strings — what would upgrade this to "strong"],
  "notes": "1–3 sentences of coaching. Reference the trader's own recent similar trades if relevant. No fluff.",
  "summary": "one-line TL;DR under 120 chars"
}
"""


@dataclass
class ParsedTrade:
    symbol: str           # Bybit format e.g. BTCUSDT
    side: str             # "long" or "short"
    entry: float
    sl: float
    tp: float
    leverage: float | None
    thesis: str | None
    error: str | None = None


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def ensure_pretrade_table() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS bybit_pretrade_checks (
        id BIGSERIAL PRIMARY KEY,
        requested_at TIMESTAMPTZ DEFAULT NOW(),
        symbol VARCHAR(20) NOT NULL,
        side VARCHAR(5) NOT NULL,
        entry_price DOUBLE PRECISION NOT NULL,
        sl_price DOUBLE PRECISION NOT NULL,
        tp_price DOUBLE PRECISION NOT NULL,
        leverage DOUBLE PRECISION,
        risk_distance_pct DOUBLE PRECISION,
        rr_ratio DOUBLE PRECISION,
        market_price DOUBLE PRECISION,
        balance_usd DOUBLE PRECISION,
        thesis TEXT,
        payload JSONB,
        score INT,
        verdict VARCHAR(20),
        report_json JSONB,
        model VARCHAR(60),
        tokens_in INT,
        tokens_out INT,
        cache_read_tokens INT,
        cache_create_tokens INT
    );
    CREATE INDEX IF NOT EXISTS idx_pretrade_symbol_side ON bybit_pretrade_checks(symbol, side, requested_at DESC);
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql)
        c.commit()


def parse_command(text: str) -> ParsedTrade:
    """Parse `/check SYMBOL side entry SL TP [lev=N] [thesis...]`.

    Returns ParsedTrade with `.error` set on failure.
    """
    # Strip command itself
    stripped = re.sub(r"^/check(?:@\w+)?\s*", "", text.strip(), flags=re.IGNORECASE)
    if not stripped:
        return ParsedTrade("", "", 0, 0, 0, None, None,
                           error="Usage: `/check BTC long 67500 66800 69000 [lev=10] [thesis…]`")

    tokens = stripped.split()
    if len(tokens) < 5:
        return ParsedTrade("", "", 0, 0, 0, None, None,
                           error="Need: SYMBOL side entry SL TP. Example: `/check BTC long 67500 66800 69000`")

    raw_symbol = tokens[0].upper().replace("/", "").replace("-", "").replace("USDT", "")
    if raw_symbol not in ALLOWED_BASES:
        return ParsedTrade("", "", 0, 0, 0, None, None,
                           error=f"Symbol `{tokens[0]}` not in allowed list: {sorted(ALLOWED_BASES)}")
    symbol = f"{raw_symbol}USDT"

    side_raw = tokens[1].lower()
    if side_raw in ("long", "buy", "l"):
        side = "long"
    elif side_raw in ("short", "sell", "s"):
        side = "short"
    else:
        return ParsedTrade("", "", 0, 0, 0, None, None,
                           error=f"Side must be long|short, got `{tokens[1]}`")

    try:
        entry = float(tokens[2])
        sl = float(tokens[3])
        tp = float(tokens[4])
    except ValueError as e:
        return ParsedTrade("", "", 0, 0, 0, None, None, error=f"Bad number: {e}")

    # Direction sanity
    if side == "long":
        if not (sl < entry < tp):
            return ParsedTrade("", "", 0, 0, 0, None, None,
                               error=f"Long requires SL<entry<TP. Got SL={sl} entry={entry} TP={tp}")
    else:
        if not (tp < entry < sl):
            return ParsedTrade("", "", 0, 0, 0, None, None,
                               error=f"Short requires TP<entry<SL. Got TP={tp} entry={entry} SL={sl}")

    leverage = None
    thesis_tokens: list[str] = []
    for t in tokens[5:]:
        if t.lower().startswith("lev="):
            try:
                leverage = float(t.split("=", 1)[1])
            except ValueError:
                pass
        else:
            thesis_tokens.append(t)
    thesis = " ".join(thesis_tokens).strip() or None

    return ParsedTrade(symbol, side, entry, sl, tp, leverage, thesis)


def fetch_bybit_context(bybit: BybitHTTP, symbol: str) -> dict:
    """Price, funding, OI, 24h stats for the symbol. All fail-soft."""
    out: dict = {"symbol": symbol}
    try:
        r = bybit.get_tickers(category="linear", symbol=symbol)
        row = (r.get("result") or {}).get("list", [{}])[0]
        out["last_price"] = float(row.get("lastPrice") or 0)
        out["funding_rate"] = float(row.get("fundingRate") or 0)
        out["next_funding_time_ms"] = int(row.get("nextFundingTime") or 0)
        out["open_interest_base"] = float(row.get("openInterest") or 0)
        out["open_interest_usd"] = float(row.get("openInterestValue") or 0)
        out["price_24h_pct"] = float(row.get("price24hPcnt") or 0)
        out["turnover_24h_usd"] = float(row.get("turnover24h") or 0)
        out["high_24h"] = float(row.get("highPrice24h") or 0)
        out["low_24h"] = float(row.get("lowPrice24h") or 0)
    except Exception as e:
        out["error"] = f"bybit ticker fetch failed: {e}"
    return out


def fetch_balance(bybit: BybitHTTP) -> dict:
    try:
        r = bybit.get_wallet_balance(accountType="UNIFIED")
        a = (r.get("result") or {}).get("list", [{}])[0]
        return {
            "equity": float(a.get("totalEquity") or 0),
            "available": float(a.get("totalAvailableBalance") or 0),
            "im_used": float(a.get("totalInitialMargin") or 0),
            "upnl": float(a.get("totalPerpUPL") or 0),
        }
    except Exception as e:
        return {"error": f"balance fetch failed: {e}"}


def fetch_similar_annotations(symbol: str, side: str, limit: int = 15) -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, opened_at, entry_price, exit_price, leverage, size,
                   setup_type, confluences, confidence, thesis_pre, lesson_post,
                   emotional_state, grade_self, pnl_usd, pnl_pct, pnl_r, status,
                   auto_setup_type, auto_grade
            FROM bybit_trade_annotations
            WHERE symbol = %s AND side = %s
            ORDER BY opened_at DESC
            LIMIT %s
            """,
            (symbol, side, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def summarize_annotations(rows: list[dict]) -> dict:
    closed = [r for r in rows if (r.get("status") or "").lower() == "closed"]
    pnls = [float(r.get("pnl_usd") or 0) for r in closed]
    wins = [p for p in pnls if p > 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(p for p in pnls if p < 0)) or 1e-9
    return {
        "n_total": len(rows),
        "n_closed": len(closed),
        "wr_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "pf": round(gross_win / gross_loss, 2) if closed else None,
        "net_pnl_usd": round(sum(pnls), 2),
        "avg_pnl_usd": round(statistics.fmean(pnls), 2) if pnls else None,
        "median_grade": statistics.mode([r.get("grade_self") for r in rows if r.get("grade_self")]) if any(r.get("grade_self") for r in rows) else None,
    }


def fetch_ml_setup_stats(pair: str, direction: str, days: int = 90) -> dict:
    """Aggregate shadow/live ml_setups for this pair + direction."""
    pair_slash = f"{pair[:-4]}/USDT" if pair.endswith("USDT") else pair
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE outcome_type IN ('filled_tp','shadow_tp')) AS tp,
                COUNT(*) FILTER (WHERE outcome_type IN ('filled_sl','shadow_sl')) AS sl,
                COUNT(*) FILTER (WHERE outcome_type IN ('filled_timeout','shadow_timeout')) AS timeout,
                AVG(pnl_pct) AS avg_pnl_pct,
                AVG(rr_ratio) AS avg_rr,
                AVG(risk_distance_pct) AS avg_risk_dist
            FROM ml_setups
            WHERE pair = %s AND direction = %s
              AND feature_version >= 4
              AND outcome_type IS NOT NULL
              AND created_at >= NOW() - (%s * INTERVAL '1 day')
            """,
            (pair_slash, direction, days),
        )
        r = cur.fetchone()
    if not r or (r[0] or 0) == 0:
        return {"n": 0}
    n, tp, sl, to, avg_pnl, avg_rr, avg_risk = r
    resolved = (tp or 0) + (sl or 0) + (to or 0)
    return {
        "n_setups": n,
        "n_resolved": resolved,
        "tp_hits": tp or 0,
        "sl_hits": sl or 0,
        "timeouts": to or 0,
        "wr_pct": round((tp or 0) / resolved * 100, 1) if resolved else None,
        "avg_pnl_pct": round(float(avg_pnl or 0), 3),
        "avg_rr": round(float(avg_rr or 0), 2),
        "avg_risk_distance_pct": round(float(avg_risk or 0), 4),
    }


def build_payload(
    parsed: ParsedTrade,
    market: dict,
    balance: dict,
    annotations: list[dict],
    ann_summary: dict,
    ml_stats: dict,
) -> dict:
    risk_pct = abs(parsed.entry - parsed.sl) / parsed.entry * 100
    reward_pct = abs(parsed.tp - parsed.entry) / parsed.entry * 100
    rr = reward_pct / risk_pct if risk_pct > 0 else 0

    # Distance from current market price
    distance_from_market_pct = None
    if market.get("last_price"):
        distance_from_market_pct = (parsed.entry - market["last_price"]) / market["last_price"] * 100

    return {
        "proposed_trade": {
            "symbol": parsed.symbol,
            "side": parsed.side,
            "entry": parsed.entry,
            "sl": parsed.sl,
            "tp": parsed.tp,
            "leverage_requested": parsed.leverage,
            "thesis": parsed.thesis,
            "risk_distance_pct": round(risk_pct, 3),
            "reward_distance_pct": round(reward_pct, 3),
            "rr_ratio": round(rr, 2),
            "distance_from_market_pct": round(distance_from_market_pct, 3) if distance_from_market_pct is not None else None,
        },
        "market": market,
        "account": balance,
        "trader_history_same_symbol_side": {
            "summary": ann_summary,
            "recent": [
                {
                    "opened_at": r["opened_at"].isoformat() if r.get("opened_at") else None,
                    "entry": r.get("entry_price"),
                    "exit": r.get("exit_price"),
                    "pnl_usd": r.get("pnl_usd"),
                    "pnl_r": r.get("pnl_r"),
                    "grade": r.get("grade_self"),
                    "auto_grade": r.get("auto_grade"),
                    "setup_type": r.get("setup_type") or r.get("auto_setup_type"),
                    "thesis_pre": r.get("thesis_pre"),
                    "lesson_post": r.get("lesson_post"),
                    "emotional_state": r.get("emotional_state"),
                }
                for r in annotations[:10]
            ],
        },
        "bot_ml_setups_same_pair_direction_90d": ml_stats,
        "meta": {"generated_at": datetime.now(tz=timezone.utc).isoformat()},
    }


async def run_check(payload: dict, model: str) -> tuple[dict, dict]:
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    user_prompt = (
        "Pre-trade payload (JSON). Return the strict JSON response per system prompt.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}"
    )
    resp = await client.messages.create(
        model=model,
        max_tokens=1500,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = resp.content[0].text if resp.content else ""
    # Strip code fences if Claude wraps
    cleaned = raw.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        brace = cleaned.find("{")
        parsed = json.loads(cleaned[brace:]) if brace >= 0 else {"error": "parse failed", "raw": raw}
    usage = resp.usage
    return parsed, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def save_check(
    parsed_trade: ParsedTrade,
    market: dict,
    balance: dict,
    payload: dict,
    report: dict,
    model: str,
    usage: dict,
) -> int:
    proposed = payload.get("proposed_trade", {})
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bybit_pretrade_checks (
                symbol, side, entry_price, sl_price, tp_price, leverage,
                risk_distance_pct, rr_ratio, market_price, balance_usd, thesis,
                payload, score, verdict, report_json, model,
                tokens_in, tokens_out, cache_read_tokens, cache_create_tokens
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                parsed_trade.symbol,
                parsed_trade.side,
                parsed_trade.entry,
                parsed_trade.sl,
                parsed_trade.tp,
                parsed_trade.leverage,
                proposed.get("risk_distance_pct"),
                proposed.get("rr_ratio"),
                market.get("last_price"),
                balance.get("equity"),
                parsed_trade.thesis,
                json.dumps(payload, default=str),
                report.get("score"),
                report.get("verdict"),
                json.dumps(report, default=str),
                model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_read_tokens", 0),
                usage.get("cache_create_tokens", 0),
            ),
        )
        row = cur.fetchone()
        c.commit()
    return row[0]


def format_telegram(parsed_trade: ParsedTrade, payload: dict, report: dict, check_id: int) -> str:
    p = payload["proposed_trade"]
    emoji = {
        "strong": "🟢",
        "ok": "🟡",
        "weak": "🟠",
        "skip": "🔴",
    }.get(str(report.get("verdict", "")).lower(), "⚪")

    lines = [
        f"{emoji} *Pre-trade check #{check_id}*",
        f"`{parsed_trade.symbol}` *{parsed_trade.side.upper()}* entry `{parsed_trade.entry}` SL `{parsed_trade.sl}` TP `{parsed_trade.tp}`",
        f"R:R `{p['rr_ratio']}` · risk `{p['risk_distance_pct']:.2f}%` · reward `{p['reward_distance_pct']:.2f}%`",
        "",
        f"*Score:* `{report.get('score','?')}/10` — *{report.get('verdict','?')}*",
        f"_{report.get('summary','')}_",
    ]

    size = report.get("size_suggestion_usd")
    lev = report.get("leverage_suggestion")
    rpct = report.get("risk_pct_of_account")
    if size or lev or rpct is not None:
        lines.append("")
        lines.append("*Sizing*")
        if size is not None:
            lines.append(f"• Size: `${float(size):,.0f}`")
        if lev is not None:
            lines.append(f"• Max safe leverage: `x{lev}`")
        if rpct is not None:
            lines.append(f"• Implied account risk: `{float(rpct):.2f}%`")

    def _bullets(label: str, items) -> None:
        if items:
            lines.append("")
            lines.append(f"*{label}*")
            for it in items[:6]:
                lines.append(f"• {it}")

    _bullets("Green flags", report.get("green_flags"))
    _bullets("Red flags", report.get("red_flags"))
    _bullets("Missing", report.get("missing"))

    notes = report.get("notes")
    if notes:
        lines.append("")
        lines.append(f"_{notes}_")

    return "\n".join(lines)


async def run_full(parsed_trade: ParsedTrade, model: str, bybit: BybitHTTP | None) -> tuple[dict, dict, int]:
    """End-to-end: fetch → payload → Claude → save. Returns (report, payload, check_id)."""
    ensure_pretrade_table()

    if bybit is None and settings.BYBIT_API_KEY and settings.BYBIT_API_SECRET:
        bybit = BybitHTTP(
            testnet=settings.BYBIT_TESTNET,
            api_key=settings.BYBIT_API_KEY,
            api_secret=settings.BYBIT_API_SECRET,
        )

    market = fetch_bybit_context(bybit, parsed_trade.symbol) if bybit else {"error": "bybit not configured"}
    balance = fetch_balance(bybit) if bybit else {"error": "bybit not configured"}
    annotations = fetch_similar_annotations(parsed_trade.symbol, parsed_trade.side)
    ann_summary = summarize_annotations(annotations)
    ml_stats = fetch_ml_setup_stats(parsed_trade.symbol, parsed_trade.side, days=90)

    payload = build_payload(parsed_trade, market, balance, annotations, ann_summary, ml_stats)
    report, usage = await run_check(payload, model=model)
    check_id = save_check(parsed_trade, market, balance, payload, report, model, usage)
    return report, payload, check_id


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", help="Full command text, e.g. '/check BTC long 67500 66800 69000'")
    parser.add_argument("--model", default=None)
    parser.add_argument("--json", action="store_true", help="Dump raw JSON report")
    args = parser.parse_args()

    parsed = parse_command(args.text)
    if parsed.error:
        print(f"ERROR: {parsed.error}")
        return 1

    model = args.model or settings.CLAUDE_MODEL_AUDIT
    report, payload, check_id = await run_full(parsed, model=model, bybit=None)

    if args.json:
        print(json.dumps({"check_id": check_id, "report": report, "payload": payload}, indent=2, default=str))
    else:
        print(format_telegram(parsed, payload, report, check_id))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
