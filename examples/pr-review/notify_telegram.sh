#!/usr/bin/env bash
# Send a Telegram message for the CAO pr-review pipeline.
#
# Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment or from a
# config file (default: ~/.aws/cli-agent-orchestrator/pr-review-telegram.env,
# override with CAO_PRR_TELEGRAM_ENV). If either is missing it NO-OPS (exit 0)
# so the pipeline never breaks when notifications aren't configured.
#
# Message text comes from $1, or from stdin if no arg. Sent as plain text.
#
# Setup:
#   1. Create a bot via @BotFather -> get the token.
#   2. Message your bot once, then find your chat id:
#        curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'
#   3. Write ~/.aws/cli-agent-orchestrator/pr-review-telegram.env (chmod 600):
#        TELEGRAM_BOT_TOKEN=123456:ABC...
#        TELEGRAM_CHAT_ID=123456789
set -uo pipefail

CFG="${CAO_PRR_TELEGRAM_ENV:-$HOME/.aws/cli-agent-orchestrator/pr-review-telegram.env}"
# shellcheck disable=SC1090
[ -f "$CFG" ] && . "$CFG"

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT="${TELEGRAM_CHAT_ID:-}"
msg="${1:-$(cat)}"

if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
  echo "[notify_telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping notify" >&2
  exit 0
fi

if curl -sS --max-time 15 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
     --data-urlencode "chat_id=${CHAT}" \
     --data-urlencode "text=${msg}" \
     -d disable_web_page_preview=true >/dev/null 2>&1; then
  echo "[notify_telegram] sent" >&2
else
  echo "[notify_telegram] send failed" >&2
fi
exit 0
