"""Weekly review of Bybit manual trades via Claude API.

Pulls last N days (default 7) of annotations + outcomes, builds a structured
prompt, and returns a markdown report stored in bybit_weekly_reviews table.

Can be run on-demand. Output also saved to docs/bybit_reviews/YYYY-WW.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor
from anthropic import AsyncAnthropic

from config.settings import settings


SYSTEM_PROMPT = """You are a senior discretionary crypto trader and trading coach reviewing a week of manual perpetual futures trades on Bybit.

Your job: find patterns (good and bad), identify leaks, grade process (not just outcome), and suggest concrete adjustments for next week.

Core principles:
- Process > outcome. A winning trade with bad reasoning is still a bad trade.
- Counter-HTF trades must justify themselves with strong confluence.
- Emotional state (FOMO, revenge, tired) is a leading indicator of leaks.
- Leverage should match confidence. x20 only for A+ setups.
- Risk 2% per trade; circuit breakers: -5% day, -10% week.

Journal v2 fields (when present — `journal_schema_version=2`):
- `clean_sample` = followed_process AND no behavioral_error. Edge claims should rest on clean samples; quote dirty-vs-clean expectancy to price indiscipline.
- `realized_r` / `mfe_r` / `mae_r` / `exit_efficiency` measure how price actually moved in R. Low exit_efficiency with high mfe_r = cutting winners; behavioral_error `held_loser` = the opposite.
- `behavioral_error` tags ARE the leaks — rank them by cost. `chain` (htf/location/mtf/trigger/structure) is the closed-vocab read; cite it instead of free text when diagnosing setup quality.

Output format (markdown):

# Week Review — <date range>

## Summary
- Net PnL, win rate, trades count, best/worst trade
- Overall grade (A-F) and one-line verdict

## Patterns Detected
- Positive patterns (repeatable wins)
- Leaks (repeatable losses / mistakes)

## Specific Trade Observations
- 2-4 most instructive trades with their lessons

## Risk & Discipline Check
- Adherence to risk rules, leverage discipline, emotional state audit
- Circuit breaker hits?

## Next Week — 3 Concrete Adjustments
- Specific, measurable actions
"""


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def ensure_reviews_table() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS bybit_weekly_reviews (
        id BIGSERIAL PRIMARY KEY,
        period_start TIMESTAMPTZ NOT NULL,
        period_end TIMESTAMPTZ NOT NULL,
        trades_count INT,
        net_pnl DOUBLE PRECISION,
        win_rate_pct DOUBLE PRECISION,
        report_md TEXT,
        model VARCHAR(50),
        tokens_in INT,
        tokens_out INT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(period_start, period_end)
    );
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql)
        c.commit()


def fetch_trades(days: int) -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM bybit_trade_annotations
            WHERE opened_at >= NOW() - (%s * INTERVAL '1 day')
            ORDER BY opened_at ASC
            """,
            (days,),
        )
        return [dict(r) for r in cur.fetchall()]


def build_user_prompt(trades: list[dict], days: int) -> str:
    closed = [t for t in trades if t.get("status") == "closed"]
    total_pnl = sum(float(t.get("pnl_usd") or 0) for t in closed)
    wins = sum(1 for t in closed if (t.get("pnl_usd") or 0) > 0)
    wr = (wins / len(closed) * 100) if closed else 0

    # v2 discipline slice — clean_sample + realized_r only meaningful on v2 rows.
    v2_closed = [t for t in closed if t.get("journal_schema_version") == 2]
    v2_clean = [t for t in v2_closed if t.get("clean_sample") is True]
    v2_reviewed = [t for t in v2_closed if t.get("followed_process") is not None]
    clean_rs = [float(t["realized_r"]) for t in v2_clean if t.get("realized_r") is not None]

    summary = {
        "period_days": days,
        "total_trades": len(trades),
        "closed": len(closed),
        "open": len(trades) - len(closed),
        "net_pnl_usd": round(total_pnl, 2),
        "win_rate_pct": round(wr, 1),
        "v2_closed": len(v2_closed),
        "v2_reviewed": len(v2_reviewed),
        "v2_clean": len(v2_clean),
        "v2_clean_expectancy_r": round(sum(clean_rs) / len(clean_rs), 3) if clean_rs else None,
    }

    trade_rows: list[dict] = []
    for t in trades:
        ctx = t.get("context_snapshot") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        htf = (ctx.get("htf_bias") or {}) if isinstance(ctx, dict) else {}
        row = {
            "id": t["id"],
            "symbol": t["symbol"],
            "side": t["side"],
            "opened_at": t["opened_at"].isoformat() if t.get("opened_at") else None,
            "entry": t.get("entry_price"),
            "exit": t.get("exit_price"),
            "leverage": t.get("leverage"),
            "size": t.get("size"),
            "status": t.get("status"),
            "pnl_usd": t.get("pnl_usd"),
            "pnl_pct": t.get("pnl_pct"),
            "setup_type": t.get("setup_type"),
            "thesis_pre": t.get("thesis_pre"),
            "lesson_post": t.get("lesson_post"),
            "emotional_state": t.get("emotional_state"),
            # v2 closed-vocab chain + R metrics + process review (the learnable signal).
            "chain": {
                "htf_bias_daily": t.get("htf_bias_daily"),
                "htf_bias_4h": t.get("htf_bias_4h"),
                "location_pd": t.get("location_pd"),
                "location_quality": t.get("location_quality"),
                "mtf_1h": t.get("mtf_1h"),
                "ltf_trigger": t.get("ltf_trigger"),
                "structure_type": t.get("structure_type"),
                "tf_aligned_count": t.get("tf_aligned_count"),
            },
            "followed_process": t.get("followed_process"),
            "clean_sample": t.get("clean_sample"),
            "technical_error": t.get("technical_error"),
            "behavioral_error": t.get("behavioral_error"),
            "realized_r": t.get("realized_r"),
            "mfe_r": t.get("mfe_r"),
            "mae_r": t.get("mae_r"),
            "exit_efficiency": t.get("exit_efficiency"),
            "context": {
                "htf_aligned": htf.get("aligned_with_trade"),
                "bias_4h": htf.get("bias_4h"),
                "bias_1h": htf.get("bias_1h"),
                "funding": ctx.get("funding") if isinstance(ctx, dict) else None,
                "oi_delta_1h": ctx.get("oi_delta_1h_pct") if isinstance(ctx, dict) else None,
                "warnings": ctx.get("warnings") if isinstance(ctx, dict) else None,
            },
        }
        trade_rows.append(row)

    return (
        f"Summary:\n{json.dumps(summary, indent=2)}\n\n"
        f"Trades (all {len(trade_rows)}):\n{json.dumps(trade_rows, indent=2, default=str)}\n\n"
        "Write the weekly review in markdown per the format in the system prompt. "
        "Be specific — cite trade IDs. Focus on patterns, not individual trade narrative unless instructive."
    )


async def generate_review(prompt: str, model: str = "claude-sonnet-4-6") -> tuple[str, int, int]:
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model,
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    text = resp.content[0].text if resp.content else ""
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def save_review(days: int, trades: list[dict], report: str, model: str, tin: int, tout: int) -> int:
    closed = [t for t in trades if t.get("status") == "closed"]
    net = sum(float(t.get("pnl_usd") or 0) for t in closed)
    wr = (sum(1 for t in closed if (t.get("pnl_usd") or 0) > 0) / len(closed) * 100) if closed else None
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=days)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bybit_weekly_reviews (
                period_start, period_end, trades_count, net_pnl, win_rate_pct,
                report_md, model, tokens_in, tokens_out
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (period_start, period_end) DO UPDATE SET
                report_md = EXCLUDED.report_md,
                trades_count = EXCLUDED.trades_count,
                net_pnl = EXCLUDED.net_pnl,
                win_rate_pct = EXCLUDED.win_rate_pct,
                model = EXCLUDED.model,
                tokens_in = EXCLUDED.tokens_in,
                tokens_out = EXCLUDED.tokens_out,
                created_at = NOW()
            RETURNING id
            """,
            (start, now, len(trades), net, wr, report, model, tin, tout),
        )
        row = cur.fetchone()
        c.commit()
    return row[0]


def write_markdown_file(report: str, days: int) -> Path:
    now = datetime.now(tz=timezone.utc)
    year, week, _ = now.isocalendar()
    out_dir = Path(__file__).resolve().parent.parent / "docs" / "bybit_reviews"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{year}-W{week:02d}.md"
    path.write_text(report)
    return path


from datetime import timedelta


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt, don't call Claude")
    args = parser.parse_args()

    if not settings.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 1

    ensure_reviews_table()
    trades = fetch_trades(args.days)
    if not trades:
        print(f"No trades in last {args.days} days. Nothing to review.")
        return 0

    prompt = build_user_prompt(trades, args.days)

    if args.dry_run:
        print("=== SYSTEM ===\n" + SYSTEM_PROMPT + "\n\n=== USER ===\n" + prompt)
        return 0

    print(f"Generating review for {len(trades)} trades over {args.days}d (model={args.model})...")
    report, tin, tout = await generate_review(prompt, model=args.model)
    rid = save_review(args.days, trades, report, args.model, tin, tout)
    path = write_markdown_file(report, args.days)
    print(f"\nReview saved: DB id={rid}, file={path}, tokens: {tin} in / {tout} out")
    print("\n" + "=" * 60)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
