"""Check public IP and alert via Telegram if it changed.

Used to monitor IP drift for exchange API whitelists (Bybit, OKX).
Run weekly via cron. State stored in /tmp/last_public_ip.txt.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "config" / ".env")

STATE_FILE = Path("/tmp/last_public_ip.txt")
IP_SERVICES = ["https://api.ipify.org", "https://ifconfig.me", "https://icanhazip.com"]


def get_public_ip() -> str:
    for url in IP_SERVICES:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                ip = resp.text.strip()
                if ip and "." in ip:
                    return ip
        except requests.RequestException:
            continue
    raise RuntimeError("all IP services failed")


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"[WARN] telegram not configured, message: {message}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException as exc:
        print(f"[ERROR] telegram send failed: {exc}")


def main() -> int:
    current = get_public_ip()
    last = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""

    if not last:
        STATE_FILE.write_text(current)
        send_telegram(f"🌐 *IP baseline set*\n`{current}`\n\nWhitelist this IP on exchange APIs.")
        print(f"baseline: {current}")
        return 0

    if current != last:
        STATE_FILE.write_text(current)
        send_telegram(
            f"⚠️ *Public IP changed*\n"
            f"Old: `{last}`\n"
            f"New: `{current}`\n\n"
            f"Update whitelist on:\n"
            f"• Bybit API key\n"
            f"• OKX API key (if applicable)"
        )
        print(f"changed: {last} -> {current}")
        return 1

    print(f"unchanged: {current}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
