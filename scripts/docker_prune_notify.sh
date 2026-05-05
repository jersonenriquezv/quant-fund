#!/usr/bin/env bash
# Weekly Docker prune with Telegram notification.
# Invoked by systemd timer (docker-prune.timer). See docs/OPERATIONS.md §Disk Bloat.

set -uo pipefail

ENV_FILE="/home/jer/quant-fund/config/.env"
HOSTNAME_TAG="$(hostname)"

if [[ -f "$ENV_FILE" ]]; then
    TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
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

disk_pct() { df -h / | awk 'NR==2 {print $5}'; }
disk_used() { df -h / | awk 'NR==2 {print $3}'; }
disk_avail() { df -h / | awk 'NR==2 {print $4}'; }

PCT_BEFORE="$(disk_pct)"
USED_BEFORE="$(disk_used)"
AVAIL_BEFORE="$(disk_avail)"

STATUS="OK"
ERRORS=""

CACHE_OUT="$(docker builder prune -af --filter until=168h 2>&1)" || { STATUS="FAIL"; ERRORS+="builder_prune; "; }
IMG_OUT="$(docker image prune -af --filter until=168h 2>&1)" || { STATUS="FAIL"; ERRORS+="image_prune; "; }

CACHE_FREED="$(echo "$CACHE_OUT" | grep -oE 'Total reclaimed space: [^$]*' | tail -1 || echo 'Total reclaimed space: 0B')"
IMG_FREED="$(echo "$IMG_OUT" | grep -oE 'Total reclaimed space: [^$]*' | tail -1 || echo 'Total reclaimed space: 0B')"

PCT_AFTER="$(disk_pct)"
USED_AFTER="$(disk_used)"
AVAIL_AFTER="$(disk_avail)"

MSG=$(cat <<EOF
🧹 *Docker prune semanal* — \`${HOSTNAME_TAG}\`

*Disco:* ${PCT_BEFORE} → ${PCT_AFTER}
*Usado:* ${USED_BEFORE} → ${USED_AFTER}
*Libre:* ${AVAIL_BEFORE} → ${AVAIL_AFTER}

*Build cache:* ${CACHE_FREED}
*Images:* ${IMG_FREED}

*Status:* ${STATUS}
EOF
)

if [[ "$STATUS" == "FAIL" ]]; then
    MSG+=$'\n*Errors:* '"${ERRORS}"
fi

send_telegram "$MSG"

[[ "$STATUS" == "OK" ]] && exit 0 || exit 1
