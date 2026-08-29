#!/usr/bin/env bash
# ============================================================
# ECOSYSTEM NOTIFIER — Alert owner of all critical situations
#
# Usage:
#   notify.sh <level> <title> <message>
#
# Levels: INFO | WARN | ALERT | CRITICAL
#
# Notification channels (configure in ~/.hermes/.env or export):
#   NOTIFY_WEBHOOK_URL   — Slack/Discord/Teams incoming webhook
#   NOTIFY_EMAIL         — destination e-mail address
#   NOTIFY_TELEGRAM_TOKEN + NOTIFY_TELEGRAM_CHAT_ID — Telegram bot
#
# All notifications are also appended to the structured log at
#   ~/.hermes/data/notifications.jsonl
# so the AI oversight-manager can read and act on them.
# ============================================================

set -eo pipefail
export LC_ALL=C

# ── Resolve home dir (same pattern used across all ecosystem scripts) ──
if [ -n "${HOME:-}" ] && [ -d "$HOME" ] && [ "$HOME" != "/" ]; then
    HERMES_HOME="$HOME"
else
    HERMES_HOME=$(getent passwd "$(whoami)" 2>/dev/null | cut -d: -f6)
    HERMES_HOME="${HERMES_HOME:-/home/hermes}"
fi

ENV_FILE="${HERMES_HOME}/.hermes/.env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE" 2>/dev/null || true

STATE_DIR="${HERMES_HOME}/.hermes/data"
mkdir -p "$STATE_DIR"
NOTIFY_LOG="${STATE_DIR}/notifications.jsonl"

LEVEL="${1:-INFO}"
TITLE="${2:-Ecosystem Notification}"
MESSAGE="${3:-No message provided}"
TS=$(date -u +%FT%TZ)
HOSTNAME_VAL=$(hostname 2>/dev/null || echo "unknown")

# ── Severity prefix for human-readable output ──
case "$LEVEL" in
    CRITICAL) PREFIX="🔴 CRITICAL" ;;
    ALERT)    PREFIX="🟠 ALERT" ;;
    WARN)     PREFIX="🟡 WARN" ;;
    *)        PREFIX="🔵 INFO" ;;
esac

FULL_MSG="${PREFIX} [${TS}] ${TITLE}: ${MESSAGE}"
echo "$FULL_MSG"

# ── 1. Structured log (always) ──
python3 - <<PYEOF 2>/dev/null || true
import json, sys
entry = {
    "ts": "${TS}",
    "level": "${LEVEL}",
    "title": "${TITLE}",
    "message": "${MESSAGE}",
    "host": "${HOSTNAME_VAL}",
}
with open("${NOTIFY_LOG}", "a") as f:
    f.write(json.dumps(entry) + "\n")
PYEOF

# ── 2. Webhook (Slack / Discord / Teams) ──
if [ -n "${NOTIFY_WEBHOOK_URL:-}" ]; then
    PAYLOAD="{\"text\":\"${FULL_MSG}\"}"
    curl -s -X POST -H 'Content-type: application/json' \
        --data "$PAYLOAD" \
        "$NOTIFY_WEBHOOK_URL" >/dev/null 2>&1 || true
fi

# ── 3. Telegram ──
if [ -n "${NOTIFY_TELEGRAM_TOKEN:-}" ] && [ -n "${NOTIFY_TELEGRAM_CHAT_ID:-}" ]; then
    curl -s -X POST \
        "https://api.telegram.org/bot${NOTIFY_TELEGRAM_TOKEN}/sendMessage" \
        -d "chat_id=${NOTIFY_TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${FULL_MSG}" >/dev/null 2>&1 || true
fi

# ── 4. Email (via sendmail / msmtp if available) ──
if [ -n "${NOTIFY_EMAIL:-}" ] && command -v sendmail >/dev/null 2>&1; then
    {
        echo "To: ${NOTIFY_EMAIL}"
        echo "Subject: [Armadillo ${LEVEL}] ${TITLE}"
        echo ""
        echo "${FULL_MSG}"
    } | sendmail -t 2>/dev/null || true
fi

exit 0
