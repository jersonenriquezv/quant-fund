"""Top-down brief push — scheduled + on-change Telegram delivery (Phase 4b).

Two modes, both single-user (settings.TELEGRAM_CHAT_ID), FREEZE-safe (read-only
analytics on existing candles, NO strategy_service / ML touch):

  push-all   One-shot. Render the brief for every manual pair and send each to
             Telegram. Wired to a 4H systemd timer (candle-close aligned).

  watch      Long-lived daemon. Poll every --interval minutes, diff each pair's
             reconciled side/confidence against a state file, and push only the
             pairs whose bias changed since the last poll. First run seeds the
             baseline silently (no push) so a restart never spams.

Usage:
  PYTHONPATH=. python scripts/topdown_push.py push-all [--dry-run]
  PYTHONPATH=. python scripts/topdown_push.py watch [--interval 15] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from config.settings import settings
from shared.logger import logger
from scripts.topdown_snapshot import PAIRS, build_brief_text, build_brief_and_state

DEFAULT_STATE_FILE = Path("/tmp/topdown_last_state.json")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send_telegram(text: str, dry_run: bool) -> bool:
    """Send one Markdown message to the configured chat. Returns True on success."""
    if dry_run:
        print(f"--- DRY-RUN message ({len(text)} chars) ---\n{text}\n")
        return True
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.error("topdown_push: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return False
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = httpx.post(TELEGRAM_API.format(token=token), json=payload, timeout=20)
        if resp.status_code != 200:
            logger.error(f"topdown_push: send failed {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as exc:
        logger.error(f"topdown_push: send error {exc}")
        return False


def cmd_push_all(args) -> int:
    sent = 0
    for pair in PAIRS:
        try:
            text = build_brief_text(pair, mode="telegram")
        except Exception as exc:
            logger.error(f"topdown_push: build failed {pair} {exc}")
            continue
        if not text:
            logger.warning(f"topdown_push: insufficient data {pair}, skipped")
            continue
        if _send_telegram(text, args.dry_run):
            sent += 1
    logger.info(f"topdown_push: push-all sent {sent}/{len(PAIRS)}")
    return 0 if sent else 1


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        logger.error(f"topdown_push: state save failed {exc}")


def _poll_once(state_file: Path, dry_run: bool) -> None:
    """One watch pass: push pairs whose reconciled side/confidence changed."""
    prev = _load_state(state_file)
    seeding = not prev  # first run on a fresh state file
    new_state = dict(prev)
    pushed = 0
    for pair in PAIRS:
        try:
            text, cur = build_brief_and_state(pair)
        except Exception as exc:
            logger.error(f"topdown_push: snapshot failed {pair} {exc}")
            continue
        if cur is None:
            continue
        new_state[pair] = cur
        if seeding:
            continue
        old = prev.get(pair)
        if old and (old.get("side") == cur["side"] and old.get("confidence") == cur["confidence"]):
            continue  # no actionable change
        old_side = (old or {}).get("side", "?")
        header = (
            f"🔄 *{pair.split('/')[0]} bias change*: "
            f"{str(old_side).upper()} → {cur['side'].upper()} ({cur['confidence']})\n\n"
        )
        if text and _send_telegram(header + text, dry_run):
            pushed += 1
    _save_state(state_file, new_state)
    if seeding:
        logger.info(f"topdown_push: watch seeded baseline for {len(new_state)} pairs (no push)")
    else:
        logger.info(f"topdown_push: watch pass pushed {pushed} changed pair(s)")


def cmd_watch(args) -> int:
    state_file = Path(args.state_file)
    interval = max(1, args.interval) * 60
    logger.info(
        f"topdown_push: watch start interval={args.interval}m state={state_file} "
        f"dry_run={args.dry_run}"
    )
    while True:
        _poll_once(state_file, args.dry_run)
        if args.once:
            return 0
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Top-down brief Telegram push")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_all = sub.add_parser("push-all", help="render + send brief for every pair")
    p_all.set_defaults(func=cmd_push_all)

    p_watch = sub.add_parser("watch", help="poll + push on bias change")
    p_watch.add_argument("--interval", type=int, default=15, help="poll interval, minutes")
    p_watch.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    p_watch.add_argument("--once", action="store_true", help="single pass then exit (testing)")
    p_watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
