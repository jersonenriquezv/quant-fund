#!/usr/bin/env python3
"""Daily status digest — ONE Telegram message summarizing how the fund is doing.

Replaces the firehose of per-event bot alerts (now muted via
BOT_TELEGRAM_ALERTS_ENABLED=false) with a single daily snapshot:

  1. Shadow activity — new setups today + resolved (TP/SL/BE/timeout) + 7d WR.
  2. Edge alerts — signal_scanner topdown_edge alerts sent today.
  3. Progress vs review — terminal outcomes under the active experiment +
     days/N remaining toward the 2026-06-08 decision gate.
  4. System health — errors in last 24h (docker logs), data freshness, bot up.

Run hourly-or-daily via systemd (docs/systemd/daily-status.timer, 12:00 UTC).
Always sends (unlike shadow_health_alert.py which is edge-triggered).

    python scripts/daily_status.py            # send
    python scripts/daily_status.py --dry-run  # print only
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from config.settings import settings
from shared.notifier import TelegramNotifier

REVIEW_DATE = _dt.date(2026, 6, 8)  # engine1/scalp decision gate (see SYSTEM_BASELINE)
RESOLVED = ("shadow_tp", "shadow_sl", "shadow_breakeven")
BOT_CONTAINER = "quant-fund-bot-1"
# Exclude benchmark/null setups from performance numbers — they are sampling
# controls, not strategies, and would dilute the WR.
_NOT_BENCH = "setup_type NOT LIKE 'bench%%' AND setup_type NOT LIKE '%%random%%'"


def _db():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=5,
    )
    conn.autocommit = True
    return conn


def _shadow_section(cur) -> str:
    # New setups created today (UTC) — strategies only, no benchmarks
    cur.execute(
        f"SELECT COUNT(*) FROM ml_setups "
        f"WHERE created_at::date = CURRENT_DATE AND {_NOT_BENCH}"
    )
    new_today = int(cur.fetchone()[0] or 0)

    # Resolved today by outcome
    cur.execute(
        f"""
        SELECT
          COUNT(*) FILTER (WHERE outcome_type='shadow_tp'),
          COUNT(*) FILTER (WHERE outcome_type='shadow_sl'),
          COUNT(*) FILTER (WHERE outcome_type='shadow_breakeven'),
          COUNT(*) FILTER (WHERE outcome_type='shadow_timeout')
        FROM ml_setups
        WHERE resolved_at::date = CURRENT_DATE AND {_NOT_BENCH}
        """
    )
    tp, sl, be, to = (int(x or 0) for x in cur.fetchone())

    # 7d WR (tp / tp+sl+be) across resolved shadows — strategies only
    cur.execute(
        f"""
        SELECT
          COUNT(*) FILTER (WHERE outcome_type='shadow_tp')::float
            / NULLIF(COUNT(*) FILTER (WHERE outcome_type IN %s), 0),
          COUNT(*) FILTER (WHERE outcome_type IN %s)
        FROM ml_setups
        WHERE resolved_at > NOW() - INTERVAL '7 days' AND {_NOT_BENCH}
        """,
        (RESOLVED, RESOLVED),
    )
    row = cur.fetchone()
    wr7 = float(row[0] or 0.0) * 100
    n7 = int(row[1] or 0)

    wr_str = f"{wr7:.0f}% (N={n7})" if n7 else "no resolved (7d)"
    return (
        f"\U0001f4ca <b>Shadow</b>\n"
        f"New today: {new_today}\n"
        f"Resolved today: {tp} TP / {sl} SL / {be} BE / {to} timeout\n"
        f"7d WR: {wr_str}"
    )


def _edge_section(cur) -> str:
    cur.execute(
        """
        SELECT pair, direction, entry, rr
        FROM signal_scanner_alerts
        WHERE auto_setup_type = 'topdown_edge'
          AND scanned_at::date = CURRENT_DATE
        ORDER BY scanned_at
        """
    )
    rows = cur.fetchall()
    if not rows:
        return "\U0001f4e1 <b>Edge alerts</b>\nNone today"
    lines = [f"  {r[0]} {r[1]} @ {float(r[2]):.6g} (R:R {float(r[3]):.1f})" for r in rows]
    return f"\U0001f4e1 <b>Edge alerts</b> ({len(rows)})\n" + "\n".join(lines)


def _progress_section(cur) -> str:
    # Terminal outcomes under the active engine1 experiment (the review cohort)
    cur.execute(
        f"""
        SELECT COUNT(*) FROM ml_setups
        WHERE experiment_id = %s AND outcome_type IN %s AND {_NOT_BENCH}
        """,
        (settings.EXPERIMENT_ID, RESOLVED),
    )
    n_term = int(cur.fetchone()[0] or 0)
    days_left = (REVIEW_DATE - _dt.date.today()).days
    n_bar = 30
    pct = min(100, int(n_term / n_bar * 100)) if n_bar else 0
    return (
        f"\U0001f3af <b>Review progress</b>\n"
        f"Experiment: <code>{settings.EXPERIMENT_ID}</code>\n"
        f"Terminal: {n_term}/{n_bar} ({pct}%)\n"
        f"Review {REVIEW_DATE.isoformat()} — {days_left}d left"
    )


def _health_section(cur) -> str:
    # Data freshness — most recent ml_setups row as a bot-processing heartbeat
    cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))/3600 FROM ml_setups")
    last_setup_h = cur.fetchone()[0]
    last_setup_h = float(last_setup_h) if last_setup_h is not None else None

    # Last shadow resolution staleness
    cur.execute(
        "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(resolved_at)))/3600 FROM ml_setups "
        "WHERE outcome_type IN %s",
        (RESOLVED,),
    )
    last_res_h = cur.fetchone()[0]
    last_res_h = float(last_res_h) if last_res_h is not None else None

    bot_up = _container_running(BOT_CONTAINER)
    errors, top_src, top_n = _error_summary_24h()

    bot_str = "✅ up" if bot_up else "\U0001f534 DOWN"
    setup_str = f"{last_setup_h:.1f}h ago" if last_setup_h is not None else "n/a"
    if errors == 0:
        err_str = "0"
    elif top_src:
        # Surface the dominant source so a benign recurring timeout reads as
        # benign, not as an alarm.
        err_str = f"⚠️ {errors} ({top_n}× {top_src})"
    else:
        err_str = f"⚠️ {errors}"
    return (
        f"\U0001f6e0️ <b>System</b>\n"
        f"Bot: {bot_str}\n"
        f"Last setup logged: {setup_str}\n"
        f"Errors 24h: {err_str}"
    )


def _container_running(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        return name in out.stdout
    except Exception:
        return False


def _error_summary_24h() -> tuple[int, str | None, int]:
    """Count ERROR/CRITICAL lines in the bot logs over 24h and find the
    dominant source module. Returns (total, top_module, top_count)."""
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", "24h", BOT_CONTAINER],
            capture_output=True, text=True, timeout=30,
        )
        text = (out.stdout or "") + (out.stderr or "")
        lines = [ln for ln in text.splitlines()
                 if "| ERROR" in ln or "| CRITICAL" in ln]
        # Loguru format: "<ts> | ERROR | module.path:func:line | message"
        counts: dict[str, int] = {}
        for ln in lines:
            parts = ln.split("|")
            if len(parts) >= 3:
                module = parts[2].strip().split(":")[0]
                counts[module] = counts.get(module, 0) + 1
        if not counts:
            return len(lines), None, 0
        top_src, top_n = max(counts.items(), key=lambda kv: kv[1])
        return len(lines), top_src, top_n
    except Exception:
        return 0, None, 0


def build_digest() -> str:
    conn = _db()
    try:
        with conn.cursor() as cur:
            parts = [
                _shadow_section(cur),
                _edge_section(cur),
                _progress_section(cur),
                _health_section(cur),
            ]
    finally:
        conn.close()
    header = f"\U0001f4cb <b>DAILY STATUS</b> — {_dt.date.today().isoformat()}"
    return header + "\n\n" + "\n\n".join(parts)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Daily status digest to Telegram.")
    ap.add_argument("--dry-run", action="store_true", help="Print instead of send.")
    args = ap.parse_args()

    msg = build_digest()
    if args.dry_run or not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID):
        print(msg)
        return 0
    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    await notifier.send(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
