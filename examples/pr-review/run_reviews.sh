#!/usr/bin/env bash
# Launch one isolated CAO session per open PR to review it.
#
# Each PR gets its OWN `cao launch` session (the pr_review_supervisor in dashboard
# mode), so reviews are isolated and run concurrently — a slow or hung review on one
# PR cannot block the others. This replaces the single-manager-with-blocking-handoffs
# approach, which serialized everything into one session.
#
# The supervisor in each session: checks the PR out into an isolated git worktree,
# fans out to the four angle reviewers, synthesizes one report with triage frontmatter,
# and writes it to pr-review-data/reviews/<pr>-<sha>.md. The dashboard renders them.
#
# Usage:
#   examples/pr-review/run_reviews.sh [--limit N] [--max-parallel K] [--repo OWNER/REPO]
#
# Defaults: limit 10 non-draft PRs, 3 concurrent sessions, awslabs/cli-agent-orchestrator.
set -euo pipefail

REPO="awslabs/cli-agent-orchestrator"
LIMIT=10
MAX_PARALLEL=3
DATA_DIR="pr-review-data"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "$DATA_DIR/reviews" "$DATA_DIR/meta"
[[ -f "$DATA_DIR/state.json" ]] || echo '{}' > "$DATA_DIR/state.json"

echo "Discovering open PRs on $REPO …"
# non-draft PRs, newest first, capped at LIMIT
mapfile -t PRS < <(gh pr list --repo "$REPO" --state open \
  --json number,isDraft,headRefOid \
  --jq '[.[] | select(.isDraft|not)] | .[:'"$LIMIT"'][] | "\(.number) \(.headRefOid)"')

echo "Will review ${#PRS[@]} PRs (limit $LIMIT, up to $MAX_PARALLEL at a time):"
printf '  #%s\n' "${PRS[@]%% *}"

launched=0
for entry in "${PRS[@]}"; do
  pr="${entry%% *}"
  sha="${entry##* }"

  # skip if already reviewed at this exact SHA (idempotent re-runs / resume)
  if [[ -f "$DATA_DIR/reviews/${pr}-${sha}.md" ]]; then
    echo "  #$pr already reviewed at $sha — skipping"
    continue
  fi

  # throttle: wait while MAX_PARALLEL review sessions (prr-*) are already running
  while [[ "$(tmux ls -F '#{session_name}' 2>/dev/null | grep -c '^cao-prr-')" -ge "$MAX_PARALLEL" ]]; do
    sleep 15
  done

  echo "  launching review session for #$pr (head $sha)…"
  # One session per PR. Two steps:
  #  1. `cao launch` starts the supervisor idle in its own session (prr-<n>).
  #  2. `cao session send --async` delivers the review task and returns immediately,
  #     so the driver can move on (subject to the MAX_PARALLEL throttle).
  # The supervisor runs in dashboard mode: checks out the PR in an isolated worktree,
  # fans out to the four reviewers, writes the report, then goes idle.
  msg="Review PR #$pr. MODE: dashboard, write report to ${DATA_DIR}/reviews/${pr}-${sha}.md"
  if cao launch --agents pr_review_supervisor --provider claude_code --yolo \
       --session-name "prr-${pr}" >/dev/null 2>&1; then
    sleep 12   # let the supervisor finish booting before sending the task
    cao session send "cao-prr-${pr}" "$msg" --async >/dev/null 2>&1 \
      || echo "    (send for #$pr failed — check 'cao session status cao-prr-${pr}')"
  else
    echo "    (launch for #$pr failed — check 'tmux ls')"
  fi
  launched=$((launched+1))
  sleep 4   # small stagger between PRs
done

echo "Launched $launched review session(s). Each PR has its own session (prr-<n>)."
echo "Watch the dashboard; reports land as each finishes. Sessions: tmux ls | grep cao-prr-"
echo "When done, reclaim sessions: for s in \$(tmux ls -F '#{session_name}' | grep '^cao-prr-'); do cao shutdown --session \"\$s\"; done"
