#!/usr/bin/env bash
# Send a per-PR "decision needed" Telegram message with inline buttons:
#   ✅ Approve & post   💬 Comment   🛑 Skip
# The pr-review-bot.py daemon listens for the tap and acts (stale-head safe).
#
# Usage: notify_telegram_decision.sh <pr> [<pr>...]
# Reads TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (env or pr-review-telegram.env).
# No-ops without creds. Skips a PR that has no report at its current head.
#
# callback_data = "prr:<pr>:<action>" (<=64 bytes); the bot re-resolves the head
# at tap time, so a moved head is handled then (not encoded here).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
REPO="${CAO_PRR_REPO:-awslabs/cli-agent-orchestrator}"
DATA_DIR="pr-review-data"

CFG="${CAO_PRR_TELEGRAM_ENV:-$HOME/.aws/cli-agent-orchestrator/pr-review-telegram.env}"
# shellcheck disable=SC1090
[ -f "$CFG" ] && . "$CFG"
TOKEN="${TELEGRAM_BOT_TOKEN:-}"; CHAT="${TELEGRAM_CHAT_ID:-}"
if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
  echo "[decision] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping" >&2; exit 0
fi

# Frontmatter scalar extractor (handles inline + block scalars > >- | |-).
fm(){ awk -v k="$2" '
  /^---[[:space:]]*$/{f++; if(f==1){i=1;next} else if(f==2)exit}
  i&&!b{ if($0~"^"k":[[:space:]]*"){ r=$0; sub("^"k":[[:space:]]*","",r);
           if(r~/^[|>][+-]?[[:space:]]*$/){b=1;next}
           gsub(/^["'"'"']|["'"'"']$/,"",r); print r; exit } next }
  b{ if($0~/^[A-Za-z_][A-Za-z0-9_]*:/){print v; exit}
     l=$0; sub(/^[[:space:]]+/,"",l); v=(v==""?l:v" "l) }
  END{ if(b) print v }' "$1"; }

for pr in "$@"; do
  head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null)"
  f="$DATA_DIR/reviews/${pr}-${head}.md"
  if [ ! -f "$f" ]; then echo "[decision] #$pr no report at current head — skip" >&2; continue; fi

  title="$(fm "$f" title)"; verdict="$(fm "$f" verdict)"
  nh="$(fm "$f" needs_human)"; summ="$(fm "$f" summary)"
  [ "${#summ}" -gt 500 ] && summ="${summ:0:500}…"
  flag=""; [ "$nh" = "true" ] && flag="  ⚠️ needs_human"
  url="https://github.com/${REPO}/pull/${pr}"
  # Primary button + callback reflect the actual verdict. The bot re-checks the
  # current-head verdict on tap and only posts if it still matches this code.
  case "$verdict" in
    *"Request changes"*) primary='{"text":"🔁 Request changes & post","callback_data":"prr:'"$pr"':post:r"}' ;;
    *Approve*)           primary='{"text":"✅ Approve & post","callback_data":"prr:'"$pr"':post:a"}' ;;
    *)                   primary='{"text":"💬 Post as comment","callback_data":"prr:'"$pr"':comment"}' ;;
  esac
  text="🔷 Decision needed — PR #${pr}
${title}
Verdict: ${verdict}${flag}

${summ}

${url}"

  kb='{"inline_keyboard":[['"$primary"',{"text":"💬 Comment","callback_data":"prr:'"$pr"':comment"},{"text":"🛑 Skip","callback_data":"prr:'"$pr"':skip"}]]}'

  if curl -sS --max-time 15 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
       --data-urlencode "chat_id=${CHAT}" \
       --data-urlencode "text=${text}" \
       --data-urlencode "reply_markup=${kb}" \
       -d disable_web_page_preview=true >/dev/null 2>&1; then
    echo "[decision] sent #$pr" >&2
  else
    echo "[decision] send failed #$pr" >&2
  fi
done
