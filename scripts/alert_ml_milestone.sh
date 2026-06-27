#!/usr/bin/env bash
#
# alert_ml_milestone.sh — Telegram alert when engine1 reaches a training milestone.
#
# Counts engine1_trend_pullback rows with a clean binary outcome
# (shadow_tp or shadow_sl, feature_version >= 4) — the exact dataset the
# ml_v0_engine1 trainer consumes. When the count crosses THRESHOLD, sends one
# Telegram message and writes a flag file so it never re-fires for that level.
#
# Designed for host cron. No bot redeploy, fully decoupled from the trading loop.
# Queries Postgres via `docker exec` (no host psql / exposed port needed).
#
# Usage:
#   scripts/alert_ml_milestone.sh           # normal check (cron)
#   THRESHOLD=400 scripts/alert_ml_milestone.sh   # override milestone
#   scripts/alert_ml_milestone.sh --test    # send a test message + report count, no flag

set -euo pipefail

REPO_DIR="/home/jer/quant-fund"
PG_CONTAINER="quant-fund-postgres-1"
THRESHOLD="${THRESHOLD:-500}"
FLAG_FILE="${REPO_DIR}/.ml_milestone_${THRESHOLD}_alerted"

# --- Load Telegram creds from .env (names only; values stay in file) ---
TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "${REPO_DIR}/config/.env" | cut -d= -f2- | tr -d '"'"'"' ')"
TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "${REPO_DIR}/config/.env" | cut -d= -f2- | tr -d '"'"'"' ')"

send_telegram() {
    local text="$1"
    curl -s -o /dev/null \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "parse_mode=HTML"
}

count_rows() {
    docker exec "${PG_CONTAINER}" psql -U jer -d quant_fund -tA -c \
        "SELECT count(*) FROM ml_setups
         WHERE setup_type='engine1_trend_pullback'
           AND feature_version>=4
           AND outcome_type IN ('shadow_tp','shadow_sl');"
}

COUNT="$(count_rows | tr -d '[:space:]')"

if [[ "${1:-}" == "--test" ]]; then
    send_telegram "🧪 ML milestone alert wired OK. engine1 binary outcomes now: <b>${COUNT}</b> / ${THRESHOLD}."
    echo "test sent — count=${COUNT} threshold=${THRESHOLD}"
    exit 0
fi

# Already alerted for this threshold — nothing to do.
[[ -f "${FLAG_FILE}" ]] && exit 0

if [[ "${COUNT}" =~ ^[0-9]+$ ]] && (( COUNT >= THRESHOLD )); then
    send_telegram "🎯 <b>ML milestone reached</b>
engine1_trend_pullback binary outcomes: <b>${COUNT}</b> (≥ ${THRESHOLD}).
Time to re-train ml_v0 + run an out-of-time check.
Run: <code>python scripts/ml_v0_engine1.py</code>"
    touch "${FLAG_FILE}"
    echo "ALERT FIRED — count=${COUNT}"
else
    echo "no alert — count=${COUNT} threshold=${THRESHOLD}"
fi
