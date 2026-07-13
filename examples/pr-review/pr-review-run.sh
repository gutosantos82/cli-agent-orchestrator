#!/usr/bin/env bash
# Detached PR-review driver wrapper, launched by pr-review-gate.sh.
#
#   notify "started" -> run the review driver -> notify "outcomes" -> release lock
#
# Args: the PR numbers being reviewed this run (for the notifications). The driver
# itself re-derives the work; these are just for the start/outcome messages.
#
# Telegram notifications are best-effort (notify_telegram.sh no-ops without creds).
# The driver command is overridable via CAO_PRR_DRIVER_CMD (used by tests).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

REPO="${CAO_PRR_REPO:-awslabs/cli-agent-orchestrator}"
LIMIT="${CAO_PRR_LIMIT:-20}"
DATA_DIR="pr-review-data"
LOCKDIR="$DATA_DIR/.driver.lock"
NOTIFY="$SCRIPT_DIR/notify_telegram.sh"

needed="$*"
pretty_list="$(echo "$needed" | tr ' ' '\n' | sed '/^$/d;s/^/#/' | paste -sd' ' - 2>/dev/null)"

# release the lock no matter how we exit
cleanup() { rm -rf "$REPO_ROOT/$LOCKDIR" 2>/dev/null || true; }
trap cleanup EXIT

echo "=== driver run started $(date -u +%FT%TZ) — PRs: $pretty_list ==="

"$NOTIFY" "🔎 CAO PR-review started — $(date -u +%FT%TZ)
Reviewing: ${pretty_list:-<none>}
Repo: $REPO" || true

# --- run the driver (overridable for tests) ---------------------------------
if [ -n "${CAO_PRR_DRIVER_CMD:-}" ]; then
  bash -c "$CAO_PRR_DRIVER_CMD" || true
else
  examples/pr-review/run_reviews.sh --limit "$LIMIT" --repo "$REPO" || true
fi

echo "=== driver run finished $(date -u +%FT%TZ) ==="

# --- build the outcomes summary from the just-reviewed PRs' reports ----------
summary="✅ CAO PR-review complete — $(date -u +%FT%TZ)"
for pr in $needed; do
  head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null)"
  f="$DATA_DIR/reviews/${pr}-${head}.md"
  if [ -f "$f" ]; then
    v="$(awk -F': ' '/^verdict:/{gsub(/"/,"",$2);print $2;exit}' "$f")"
    nh="$(awk -F': ' '/^needs_human:/{gsub(/[ "]/,"",$2);print $2;exit}' "$f")"
    s="$(awk -F': ' '/^summary:/{gsub(/"/,"",$2);print $2;exit}' "$f")"
    flag=""; [ "$nh" = "true" ] && flag=" ⚠️needs-human"
    summary="$summary

#$pr — ${v:-?}${flag}
  ${s:-}"
  else
    summary="$summary

#$pr — no report (head moved or review did not complete)"
  fi
done

printf '%s\n' "$summary" | "$NOTIFY" || true
echo "$summary"
