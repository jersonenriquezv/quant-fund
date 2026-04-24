"""Weekly edge audit via Claude Opus 4.7 (1M context).

Dumps resolved ml_setups, closed live trades, and shadow outcomes from the
last N days into aggregated stats + per-feature splits + top/bottom trade
detail, then asks Claude to produce a narrative audit. Stores the report in
ml_edge_audits and writes a markdown file to docs/audits/.

Intended to run weekly via systemd timer. Can be invoked on-demand.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor
from anthropic import AsyncAnthropic

from config.settings import settings
from data_service.data_store import ml_market_outcome_filter_sql


SYSTEM_PROMPT = """You are a senior quantitative researcher auditing a Smart Money Concepts (SMC) crypto trading bot.

The bot runs 7 linear perpetuals on OKX (BTC/ETH/SOL/DOGE/XRP/LINK/AVAX). Each setup is a deterministic detection (BOS/CHoCH + OB/FVG + sweep + confluences). Setups are logged with ~40 features in `ml_setups`, then resolved against the market (shadow mode tracks TP/SL/timeout without executing, live mode executes).

Your job: find edge and leaks, separate signal from noise, grade feature predictive power, and recommend concrete parameter or gating changes. You are NOT picking trades; you are auditing the population.

Core principles:
- Sample sizes matter. Call out when a claimed effect has n<20 — label it "hypothesis" not "finding".
- Distinguish MARKET outcomes (filled_tp/sl/timeout, shadow_tp/sl/timeout) from BOOKKEEPING (already filtered out of input).
- WR alone lies. Always cite PF, avg R-multiple, and hold duration.
- Shadow vs live drift is a first-class signal. If shadow WR >> live WR for the same setup, fill/slippage/execution is leaking edge.
- Feature tiers (confluence_count, pd_aligned, sweep_tier, funding_tier, has_oi_flush, cvd_aligned) split the population — show which slice the edge lives in.
- Regime context: `htf_bias` (long/short/undefined), `hour_of_day`, `atr_pct`, `daily_vol`. Edge often concentrates in specific regimes.
- Recommend changes that are MEASURABLE (a threshold, a gate, a disabled setup) — not vague directionals.

Output format (markdown):

# Edge Audit — <date range>

## TL;DR
- 2–3 sentence verdict. Is the bot's edge growing, flat, or decaying? What's the single most actionable change?

## Population Overview
- Counts: total setups, resolved, by shadow/live split
- Overall WR, PF, avg pnl_pct, median hold
- Trust band: is sample size enough for the claims in this audit?

## Per-Setup Performance
- Table: setup_type | n | WR | PF | avg pnl% | median hold
- Which setups are carrying the edge? Which are dead weight?

## Feature Edge Slices
- Which feature tiers concentrate WR above baseline? Which slice *destroys* edge?
- Cite n per slice. Skip slices with n<15.

## Shadow vs Live Delta
- For setups with both: shadow WR vs live WR, gap, likely cause (fill, slippage, timing).

## Regime Edge
- htf_bias buckets, hour_of_day buckets, vol regime
- Where does the bot make money? Where does it bleed?

## Instructive Trades
- 2–4 of the top winners + 2–4 of the top losers
- What feature combo was present? What's the takeaway?

## Leaks Detected
- Concrete patterns of losing money. Each with: evidence (n, WR delta), hypothesis, proposed fix.

## 3 Concrete Adjustments
- Specific, measurable, testable. Each with expected impact and how to verify.

## Open Questions
- What the data cannot yet answer. What to collect more of.
"""


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def ensure_edge_audits_table() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS ml_edge_audits (
        id BIGSERIAL PRIMARY KEY,
        period_start TIMESTAMPTZ NOT NULL,
        period_end TIMESTAMPTZ NOT NULL,
        experiment_id VARCHAR(100),
        feature_version_min INT,
        n_setups INT,
        n_live_trades INT,
        n_shadow INT,
        net_pnl_usd DOUBLE PRECISION,
        win_rate_pct DOUBLE PRECISION,
        report_md TEXT,
        payload JSONB,
        model VARCHAR(60),
        tokens_in INT,
        tokens_out INT,
        cache_read_tokens INT,
        cache_create_tokens INT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(period_start, period_end, experiment_id)
    );
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql)
        c.commit()


def fetch_ml_setups(days: int, experiment_id: str, feature_version_min: int) -> list[dict]:
    market_filter = ml_market_outcome_filter_sql()
    sql = f"""
        SELECT *
        FROM ml_setups
        WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
          AND feature_version >= %s
          AND experiment_id = %s
          AND outcome_type IS NOT NULL
          AND {market_filter}
        ORDER BY created_at ASC
    """
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (days, feature_version_min, experiment_id))
        return [dict(r) for r in cur.fetchall()]


def fetch_live_trades(days: int) -> list[dict]:
    sql = """
        SELECT *
        FROM trades
        WHERE closed_at >= NOW() - (%s * INTERVAL '1 day')
          AND status = 'closed'
        ORDER BY closed_at ASC
    """
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (days,))
        return [dict(r) for r in cur.fetchall()]


def _is_win(row: dict) -> bool:
    pnl = row.get("pnl_pct") or row.get("pnl_usd") or 0
    try:
        return float(pnl) > 0
    except (TypeError, ValueError):
        return False


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    pnls = [float(r.get("pnl_pct") or 0) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    holds = [float(r.get("trade_duration_ms") or 0) / 60000.0 for r in rows if r.get("trade_duration_ms")]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses)) or 1e-9
    return {
        "n": len(rows),
        "wr_pct": round(len(wins) / len(rows) * 100, 1),
        "avg_pnl_pct": round(statistics.fmean(pnls), 3) if pnls else 0,
        "median_pnl_pct": round(statistics.median(pnls), 3) if pnls else 0,
        "pf": round(gross_win / gross_loss, 2),
        "median_hold_min": round(statistics.median(holds), 1) if holds else None,
    }


def _group(rows: list[dict], key: str, bucket=None) -> dict:
    out: dict[str, list[dict]] = {}
    for r in rows:
        v = r.get(key)
        if bucket is not None:
            v = bucket(v)
        if v is None:
            v = "null"
        out.setdefault(str(v), []).append(r)
    return {k: _stats(v) for k, v in sorted(out.items())}


def _hour_bucket(h) -> str:
    if h is None:
        return "null"
    try:
        h = int(h)
    except (TypeError, ValueError):
        return "null"
    if 0 <= h < 6:
        return "00-06_asia"
    if 6 <= h < 12:
        return "06-12_eu"
    if 12 <= h < 18:
        return "12-18_us"
    return "18-24_late_us"


def _confluence_bucket(c) -> str:
    try:
        c = int(c or 0)
    except (TypeError, ValueError):
        return "null"
    if c <= 1:
        return "1"
    if c == 2:
        return "2"
    if c == 3:
        return "3"
    return "4+"


def shadow_vs_live_delta(setups: list[dict]) -> dict:
    shadow = [r for r in setups if (r.get("outcome_type") or "").startswith("shadow_")]
    live = [r for r in setups if (r.get("outcome_type") or "").startswith("filled_")]
    by_type_shadow = _group(shadow, "setup_type")
    by_type_live = _group(live, "setup_type")
    keys = sorted(set(by_type_shadow) | set(by_type_live))
    return {
        k: {
            "shadow": by_type_shadow.get(k, {"n": 0}),
            "live": by_type_live.get(k, {"n": 0}),
        }
        for k in keys
    }


FEATURE_KEYS_TO_DUMP = [
    "setup_id", "pair", "direction", "setup_type", "timestamp",
    "entry_price", "sl_price", "tp1_price", "tp2_price",
    "risk_distance_pct", "rr_ratio", "entry_distance_pct",
    "confluence_count", "htf_bias", "pd_zone", "pd_aligned",
    "has_liquidity_sweep", "has_choch", "has_bos", "has_fvg",
    "sweep_tier", "funding_tier", "oi_rising_tier", "dominance_tier",
    "has_oi_flush", "oi_flush_usd", "cvd_aligned", "funding_extreme",
    "funding_rate", "oi_usd", "cvd_5m", "cvd_15m", "cvd_1h",
    "buy_dominance", "fear_greed_score", "hour_of_day",
    "atr_pct", "daily_vol", "shadow_mode",
    "outcome_type", "pnl_pct", "pnl_usd", "exit_reason",
    "fill_duration_ms", "trade_duration_ms",
]


def _trim_row(r: dict) -> dict:
    return {k: r.get(k) for k in FEATURE_KEYS_TO_DUMP}


def top_bottom(rows: list[dict], k: int = 10) -> dict:
    ranked = sorted(rows, key=lambda r: float(r.get("pnl_pct") or 0))
    return {
        "top_losers": [_trim_row(r) for r in ranked[:k]],
        "top_winners": [_trim_row(r) for r in ranked[-k:][::-1]],
    }


def build_payload(days: int, setups: list[dict], live_trades: list[dict], experiment_id: str) -> dict:
    shadow = [r for r in setups if (r.get("outcome_type") or "").startswith("shadow_")]
    live = [r for r in setups if (r.get("outcome_type") or "").startswith("filled_")]

    return {
        "meta": {
            "period_days": days,
            "experiment_id": experiment_id,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "counts": {
                "resolved_setups": len(setups),
                "shadow": len(shadow),
                "live_resolved": len(live),
                "live_closed_trades_table": len(live_trades),
            },
        },
        "overall": _stats(setups),
        "by_setup_type": _group(setups, "setup_type"),
        "by_pair": _group(setups, "pair"),
        "by_direction": _group(setups, "direction"),
        "by_htf_bias": _group(setups, "htf_bias"),
        "by_hour": _group(setups, "hour_of_day", bucket=_hour_bucket),
        "by_confluence_count": _group(setups, "confluence_count", bucket=_confluence_bucket),
        "by_pd_aligned": _group(setups, "pd_aligned"),
        "by_sweep_tier": _group(setups, "sweep_tier"),
        "by_funding_tier": _group(setups, "funding_tier"),
        "by_oi_rising_tier": _group(setups, "oi_rising_tier"),
        "by_has_oi_flush": _group(setups, "has_oi_flush"),
        "by_cvd_aligned": _group(setups, "cvd_aligned"),
        "shadow_vs_live": shadow_vs_live_delta(setups),
        "samples": top_bottom(setups, k=10),
    }


async def generate_audit(payload: dict, model: str) -> tuple[str, dict]:
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    user_prompt = (
        "Aggregated audit payload (JSON). Write the markdown audit per the system prompt. "
        "Cite concrete numbers from the payload. Call out low-sample slices.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}"
    )
    resp = await client.messages.create(
        model=model,
        max_tokens=6000,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = resp.content[0].text if resp.content else ""
    usage = resp.usage
    return text, {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_create_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def save_audit(
    period_start: datetime,
    period_end: datetime,
    experiment_id: str,
    feature_version_min: int,
    payload: dict,
    report: str,
    model: str,
    usage: dict,
) -> int:
    meta = payload.get("meta", {})
    counts = meta.get("counts", {})
    overall = payload.get("overall", {})
    net_pnl = sum(
        float(r.get("pnl_usd") or 0)
        for r in payload.get("samples", {}).get("top_winners", [])
        + payload.get("samples", {}).get("top_losers", [])
    )
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ml_edge_audits (
                period_start, period_end, experiment_id, feature_version_min,
                n_setups, n_live_trades, n_shadow,
                net_pnl_usd, win_rate_pct, report_md, payload,
                model, tokens_in, tokens_out, cache_read_tokens, cache_create_tokens
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (period_start, period_end, experiment_id) DO UPDATE SET
                report_md = EXCLUDED.report_md,
                payload = EXCLUDED.payload,
                n_setups = EXCLUDED.n_setups,
                n_live_trades = EXCLUDED.n_live_trades,
                n_shadow = EXCLUDED.n_shadow,
                net_pnl_usd = EXCLUDED.net_pnl_usd,
                win_rate_pct = EXCLUDED.win_rate_pct,
                model = EXCLUDED.model,
                tokens_in = EXCLUDED.tokens_in,
                tokens_out = EXCLUDED.tokens_out,
                cache_read_tokens = EXCLUDED.cache_read_tokens,
                cache_create_tokens = EXCLUDED.cache_create_tokens,
                created_at = NOW()
            RETURNING id
            """,
            (
                period_start,
                period_end,
                experiment_id,
                feature_version_min,
                counts.get("resolved_setups", 0),
                counts.get("live_closed_trades_table", 0),
                counts.get("shadow", 0),
                net_pnl,
                overall.get("wr_pct"),
                report,
                json.dumps(payload, default=str),
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


def write_markdown_file(report: str) -> Path:
    now = datetime.now(tz=timezone.utc)
    year, week, _ = now.isocalendar()
    out_dir = Path(__file__).resolve().parent.parent / "docs" / "audits"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"edge-audit-{year}-W{week:02d}.md"
    path.write_text(report)
    return path


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--experiment-id", default=None, help="Overrides settings.EXPERIMENT_ID")
    parser.add_argument("--feature-version-min", type=int, default=4)
    parser.add_argument("--model", default=None, help="Overrides settings.CLAUDE_MODEL_AUDIT")
    parser.add_argument("--dry-run", action="store_true", help="Build payload, skip Claude call")
    parser.add_argument("--min-setups", type=int, default=10, help="Bail if fewer resolved setups in window")
    args = parser.parse_args()

    if not settings.ANTHROPIC_API_KEY and not args.dry_run:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return 1

    experiment_id = args.experiment_id or settings.EXPERIMENT_ID
    model = args.model or settings.CLAUDE_MODEL_AUDIT

    ensure_edge_audits_table()
    setups = fetch_ml_setups(args.days, experiment_id, args.feature_version_min)
    live_trades = fetch_live_trades(args.days)

    if len(setups) < args.min_setups and not args.dry_run:
        print(f"Only {len(setups)} resolved setups in last {args.days}d (min {args.min_setups}). Skipping audit.")
        return 0

    payload = build_payload(args.days, setups, live_trades, experiment_id)

    if args.dry_run:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(
        f"Generating edge audit: {len(setups)} resolved setups, "
        f"{len(live_trades)} closed trades, model={model}, experiment={experiment_id}"
    )
    report, usage = await generate_audit(payload, model=model)

    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=args.days)
    aid = save_audit(start, now, experiment_id, args.feature_version_min, payload, report, model, usage)
    path = write_markdown_file(report)
    print(
        f"\nAudit saved: id={aid}, file={path}\n"
        f"Tokens: in={usage['input_tokens']} out={usage['output_tokens']} "
        f"cache_read={usage['cache_read_tokens']} cache_create={usage['cache_create_tokens']}"
    )
    print("\n" + "=" * 60)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
