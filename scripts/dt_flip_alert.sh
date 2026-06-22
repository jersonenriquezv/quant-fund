#!/usr/bin/env bash
# Dual Thrust shadow forward-flip alert.
# Cron every 4h (aligned past ETH 4h candle close). Telegram ONLY on change.
#
# Tracks the latest DT_SHADOW_STATE line (position/entry/trades; balance dropped
# because it drifts a few cents each candle from funding). Sends a Telegram alert
# when the position, entry, or trade count changes vs the last seen state —
# i.e. a real forward flip / close / SL hit. Silent otherwise.
#
# Install:  10 */4 * * * /home/jer/quant-fund/scripts/dt_flip_alert.sh
# State file: /tmp/dt_flip_alert_state (first run stores baseline, no alert)

set -uo pipefail

PROJECT_DIR="/home/jer/quant-fund"
ENV_FILE="$PROJECT_DIR/config/.env"
STATE_FILE="/tmp/dt_flip_alert_state"
CONTAINER="quant-fund-bot-1"

if [[ -f "$ENV_FILE" ]]; then
    TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
fi

send_telegram() {
    local text="$1"
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
        echo "telegram creds missing — skipping notification" >&2
        return 0
    fi
    curl -sS --max-time 10 \
        -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "parse_mode=Markdown" \
        > /dev/null || echo "telegram send failed" >&2
}

# Latest DT_SHADOW_STATE line across all main logs (incl. gzipped rotations)
STATE_LINE="$(docker exec "$CONTAINER" sh -c 'zgrep -h "DT_SHADOW_STATE" /app/logs/main_*.log* 2>/dev/null | tail -1')"

if [[ -z "$STATE_LINE" ]]; then
    CUR="NO_STATE"   # DT shadow stopped logging — worth knowing
else
    CUR="$(echo "$STATE_LINE" | sed -E 's/.*(signal=[A-Z]+) (position=[A-Z]+) (entry=[0-9.]+) .*(trades=[0-9]+).*/\1 \2 \3 \4/')"
fi

PREV="$(cat "$STATE_FILE" 2>/dev/null || echo '')"

# No change — stay silent (zero noise, zero tokens)
if [[ "$CUR" == "$PREV" ]]; then
    exit 0
fi

echo "$CUR" > "$STATE_FILE"

# First run: store baseline, do not alert
if [[ -z "$PREV" ]]; then
    echo "baseline stored: $CUR" >&2
    exit 0
fi

LAST_TRADE="$(docker exec "$CONTAINER" sh -c 'zgrep -h "DT_SHADOW_TRADE" /app/logs/main_*.log* 2>/dev/null | tail -1')"

MSG=$(cat <<EOF
🔔 *DT shadow CHANGED* — ETH 4h

*Before:* \`${PREV}\`
*Now:* \`${CUR}\`

*Last trade line:*
\`${LAST_TRADE}\`

Gate to Phase 1c = 3-5 real forward flips. Inspect this one.
EOF
)

send_telegram "$MSG"
exit 0
