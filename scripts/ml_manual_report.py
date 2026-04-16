"""ML bridge: analyze manual Bybit trades against bot ML features.

- Agreement analysis: when I entered, did bot's shadow detect a setup?
- Feature-outcome correlation: which context flags predict wins?
- Grade-outcome audit: is my self-grade correlated with pnl?

Run on-demand. Prints markdown report.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor

from config.settings import settings


def _conn():
    return psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )


def fetch_manual_trades(since: str) -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM bybit_trade_annotations
            WHERE opened_at >= %s AND status = 'closed'
            ORDER BY opened_at ASC
            """,
            (since,),
        )
        return [dict(r) for r in cur.fetchall()]


def bybit_to_pair(sym: str) -> str:
    return f"{sym[:-4]}/USDT" if sym.endswith("USDT") else sym


def find_bot_setups_near(pair: str, opened_at, window_sec: int = 600) -> list[dict]:
    """Look for bot ml_setups rows within ±window_sec of manual trade open."""
    with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT setup_id, setup_type, direction, created_at, outcome_type,
                   pnl_usd, risk_approved, risk_reject_reason, confluence_count
            FROM ml_setups
            WHERE pair = %s
              AND created_at BETWEEN (%s::timestamptz - (%s * INTERVAL '1 second'))
                                 AND (%s::timestamptz + (%s * INTERVAL '1 second'))
            ORDER BY created_at ASC
            """,
            (pair, opened_at, window_sec, opened_at, window_sec),
        )
        return [dict(r) for r in cur.fetchall()]


def analyze(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {}
    wins = [t for t in trades if (t.get("pnl_usd") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl_usd") or 0) < 0]
    total_pnl = sum(float(t.get("pnl_usd") or 0) for t in trades)

    # context feature → outcome
    ctx_stats: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    agreement_rows: list[dict] = []

    for t in trades:
        ctx = t.get("context_snapshot") or {}
        if isinstance(ctx, str):
            try: ctx = json.loads(ctx)
            except Exception: ctx = {}
        pnl = float(t.get("pnl_usd") or 0)

        htf = ctx.get("htf_bias") or {}
        aligned = htf.get("aligned_with_trade")
        ctx_stats["htf_aligned"][str(aligned)].append(pnl)

        ml = ctx.get("ml_features") or {}
        if ml.get("rsi_zone"):
            ctx_stats["rsi_zone"][ml["rsi_zone"]].append(pnl)
        if ml.get("adx_strength"):
            ctx_stats["adx_strength"][ml["adx_strength"]].append(pnl)
        if ml.get("bb_squeeze") is not None:
            ctx_stats["bb_squeeze"][str(ml["bb_squeeze"])].append(pnl)

        if t.get("session") or ctx.get("session"):
            ctx_stats["session"][ctx.get("session") or "unknown"].append(pnl)

        if t.get("emotional_state"):
            ctx_stats["emotional"][t["emotional_state"]].append(pnl)

        if t.get("grade_self"):
            ctx_stats["self_grade"][t["grade_self"]].append(pnl)

        # bot agreement
        pair = bybit_to_pair(t["symbol"])
        bot_setups = find_bot_setups_near(pair, t["opened_at"], window_sec=1800)
        my_dir = "long" if t["side"] == "Buy" else "short"
        matching = [s for s in bot_setups if s.get("direction") == my_dir]
        agreement_rows.append({
            "manual_id": t["id"],
            "symbol": t["symbol"],
            "side": t["side"],
            "opened": t["opened_at"].isoformat(),
            "pnl": round(pnl, 2),
            "bot_detected": len(bot_setups),
            "bot_matching_dir": len(matching),
            "bot_setup_types": sorted({s["setup_type"] for s in matching}),
        })

    def group_summary(group: dict[str, list[float]]) -> list[dict]:
        rows = []
        for key, pnls in group.items():
            if not pnls: continue
            wr = 100 * sum(1 for x in pnls if x > 0) / len(pnls)
            rows.append({
                "key": key,
                "n": len(pnls),
                "net_pnl": round(sum(pnls), 2),
                "avg_pnl": round(sum(pnls) / len(pnls), 2),
                "win_rate": round(wr, 1),
            })
        return sorted(rows, key=lambda r: -r["n"])

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100 * len(wins) / n, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / n, 2),
        "by_feature": {k: group_summary(v) for k, v in ctx_stats.items()},
        "agreement": agreement_rows,
    }


def render_md(a: dict, since: str) -> str:
    if not a:
        return f"# ML Manual Report (since {since})\n\nNo closed trades.\n"
    lines = [
        f"# ML Manual Report · since {since}",
        "",
        f"**{a['n']} trades** · {a['wins']}W/{a['losses']}L · WR {a['win_rate']}% · PnL ${a['total_pnl']:+.2f} (avg ${a['avg_pnl']:+.2f})",
        "",
    ]

    for name, label in [
        ("htf_aligned", "HTF Alignment"),
        ("session", "Trading Session"),
        ("rsi_zone", "RSI Zone"),
        ("adx_strength", "ADX Strength"),
        ("bb_squeeze", "BB Squeeze"),
        ("emotional", "Emotional State"),
        ("self_grade", "Self Grade"),
    ]:
        rows = a["by_feature"].get(name) or []
        if not rows: continue
        lines.append(f"## {label}")
        lines.append("| key | n | WR | avg $ | net $ |")
        lines.append("|---|---|---|---|---|")
        for r in rows:
            lines.append(f"| {r['key']} | {r['n']} | {r['win_rate']}% | {r['avg_pnl']} | {r['net_pnl']} |")
        lines.append("")

    lines.append("## Bot Agreement")
    lines.append("| Manual # | Symbol | Side | Opened | PnL $ | Bot detected | Bot matching dir | Types |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in a["agreement"]:
        types = ", ".join(row["bot_setup_types"]) or "—"
        lines.append(f"| #{row['manual_id']} | {row['symbol']} | {row['side']} | {row['opened']} | {row['pnl']} | {row['bot_detected']} | {row['bot_matching_dir']} | {types} |")

    agreed = sum(1 for r in a["agreement"] if r["bot_matching_dir"] > 0)
    lines.append("")
    lines.append(f"**Agreement rate:** {agreed}/{a['n']} ({100 * agreed / a['n']:.1f}%) manual trades had a bot setup in same direction within ±30min.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-04-01", help="Filter trades opened on/after this date (YYYY-MM-DD)")
    parser.add_argument("--md", help="Write markdown report to this path")
    args = parser.parse_args()

    trades = fetch_manual_trades(args.since)
    if not trades:
        print(f"No closed trades since {args.since}.")
        return 0

    analysis = analyze(trades)
    report = render_md(analysis, args.since)
    print(report)

    if args.md:
        Path(args.md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md).write_text(report)
        print(f"\nWritten: {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
