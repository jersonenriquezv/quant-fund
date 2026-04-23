#!/usr/bin/env python3
"""
Shadow health alert — queries DB for known regression signals and sends
Telegram alerts if thresholds are breached.

Designed to run via cron every hour:
    0 * * * * cd /home/jer/quant-fund && ./venv/bin/python scripts/shadow_health_alert.py

Thresholds (all configurable via env):
- SHADOW_BE_RATE_ALERT (default 0.50): breakeven rate > threshold on rolling 7d
- SHADOW_ORPHAN_ALERT (default 5): shadow_orphaned rows in last 24h
- SHADOW_STALE_ALERT (default 48): no new resolved outcomes in N hours

State file: /tmp/shadow_health_alert_state.json prevents duplicate alerts —
each breach is only notified once until it clears.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

from config.settings import settings
from shared.notifier import TelegramNotifier


STATE_PATH = Path("/tmp/shadow_health_alert_state.json")

BE_RATE_ALERT = float(os.getenv("SHADOW_BE_RATE_ALERT", "0.50"))
ORPHAN_ALERT = int(os.getenv("SHADOW_ORPHAN_ALERT", "5"))
STALE_HOURS = int(os.getenv("SHADOW_STALE_ALERT", "48"))


@dataclass
class Check:
    name: str
    breached: bool
    value: float | int
    message: str


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def _db():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST, port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD, connect_timeout=5,
    )
    conn.autocommit = True
    return conn


def check_be_rate(cur) -> Check:
    cur.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE outcome_type='shadow_breakeven')::float /
          NULLIF(COUNT(*) FILTER (WHERE outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven')), 0) AS be_rate,
          COUNT(*) FILTER (WHERE outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven')) AS n
        FROM ml_setups
        WHERE experiment_id = %s
          AND created_at > NOW() - INTERVAL '7 days'
        """,
        (settings.EXPERIMENT_ID,),
    )
    row = cur.fetchone()
    be_rate = float(row[0] or 0.0)
    n = int(row[1] or 0)
    breached = n >= 10 and be_rate > BE_RATE_ALERT
    msg = (
        f"\u26a0\ufe0f <b>BE rate high</b>\n"
        f"experiment: <code>{settings.EXPERIMENT_ID}</code>\n"
        f"BE rate: <b>{be_rate*100:.1f}%</b> (bar {BE_RATE_ALERT*100:.0f}%)\n"
        f"N resolved: {n}"
    )
    return Check("be_rate_high", breached, be_rate, msg)


def check_orphans(cur) -> Check:
    cur.execute(
        """
        SELECT COUNT(*) FROM ml_setups
        WHERE outcome_type = 'shadow_orphaned'
          AND resolved_at > NOW() - INTERVAL '24 hours'
        """
    )
    n = int(cur.fetchone()[0] or 0)
    breached = n > ORPHAN_ALERT
    msg = (
        f"\u26a0\ufe0f <b>Shadow orphans spiking</b>\n"
        f"Orphans in last 24h: <b>{n}</b> (bar {ORPHAN_ALERT})\n"
        f"Likely restart losing Redis state. Investigate shadow_monitor._save_to_redis."
    )
    return Check("orphans_high", breached, n, msg)


def check_stale(cur) -> Check:
    cur.execute(
        """
        SELECT EXTRACT(EPOCH FROM (NOW() - MAX(resolved_at))) / 3600.0 AS hours_since
        FROM ml_setups
        WHERE outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven')
        """
    )
    row = cur.fetchone()
    hours = float(row[0] or 0.0)
    breached = hours > STALE_HOURS
    msg = (
        f"\u26a0\ufe0f <b>No shadow resolutions recently</b>\n"
        f"Last resolved: <b>{hours:.1f}h</b> ago (bar {STALE_HOURS}h)\n"
        f"Check if bot is processing candles: <code>docker compose logs bot --tail=50</code>"
    )
    return Check("stale_outcomes", breached, hours, msg)


async def main():
    conn = _db()
    checks: list[Check] = []
    try:
        with conn.cursor() as cur:
            checks.append(check_be_rate(cur))
            checks.append(check_orphans(cur))
            checks.append(check_stale(cur))
    finally:
        conn.close()

    state = _load_state()
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("WARN: Telegram token/chat_id not set — printing alerts only, not sending")
        notifier = None
    else:
        notifier = TelegramNotifier(token=token, chat_id=chat_id)
    any_alert = False
    for check in checks:
        was_breached = state.get(check.name, False)
        if check.breached and not was_breached:
            any_alert = True
            if notifier:
                await notifier.send(check.message)
            else:
                print(f"WOULD SEND: {check.message}")
            state[check.name] = True
        elif not check.breached and was_breached:
            msg = (
                f"\u2705 <b>{check.name} cleared</b>\n"
                f"Current value: {check.value}"
            )
            if notifier:
                await notifier.send(msg)
            else:
                print(f"WOULD SEND: {msg}")
            state[check.name] = False

    _save_state(state)
    # Always print status so cron log captures it
    for check in checks:
        status = "BREACH" if check.breached else "ok"
        print(f"[{status}] {check.name}: {check.value}")


if __name__ == "__main__":
    asyncio.run(main())
