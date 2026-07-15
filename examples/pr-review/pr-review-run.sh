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
# Extract a frontmatter scalar by key, handling inline values ("x" / x) AND
# YAML block scalars (>, >-, |, |-) whose text sits on following indented lines.
fm_scalar() {  # $1=file  $2=key
  awk -v key="$2" '
    /^---[[:space:]]*$/ { fm++; if (fm==1){infm=1; next} else if (fm==2){exit} }
    infm && !inblock {
      if ($0 ~ "^" key ":[[:space:]]*") {
        rest=$0; sub("^" key ":[[:space:]]*","",rest)
        if (rest ~ /^[|>][+-]?[[:space:]]*$/) { inblock=1; next }   # block scalar
        gsub(/^["'"'"']|["'"'"']$/,"",rest); print rest; exit       # inline: strip quotes
      }
      next
    }
    inblock {
      if ($0 ~ /^[A-Za-z_][A-Za-z0-9_]*:/) { print val; exit }      # next top-level key
      line=$0; sub(/^[[:space:]]+/,"",line)
      val=(val=="" ? line : val " " line)
    }
    END { if (inblock) print val }
  ' "$1"
}

summary="✅ CAO PR-review complete — $(date -u +%FT%TZ)"
for pr in $needed; do
  head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null)"
  f="$DATA_DIR/reviews/${pr}-${head}.md"
  if [ -f "$f" ]; then
    v="$(fm_scalar "$f" verdict)"
    nh="$(fm_scalar "$f" needs_human)"
    s="$(fm_scalar "$f" summary)"
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

# For approves the guard would HOLD (needs_human / sensitive / large), send a
# per-PR Telegram decision message with Approve/Comment/Skip buttons. The
# pr-review-bot.py daemon acts on the tap. Reuses the guard via --dry-run so the
# "held" set is exactly what would be gated at publish time.
if [ -n "$needed" ]; then
  held="$(examples/pr-review/publish_reviews.sh --dry-run $needed 2>/dev/null \
           | awk '/HOLD: approve gated/{n=$1; sub(/^#/,"",n); print n}')"
  if [ -n "$held" ]; then
    echo "=== decision messages for held approves: $held ===" >&2
    examples/pr-review/notify_telegram_decision.sh $held || true
  fi
fi
