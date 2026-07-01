#!/usr/bin/env bash
# Launch one isolated CAO session per open PR to review it.
#
# Each PR gets its OWN `cao launch` session (the pr_review_supervisor in dashboard
# mode), so reviews are isolated and run concurrently — a slow or hung review on one
# PR cannot block the others. This replaces the single-manager-with-blocking-handoffs
# approach, which serialized everything into one session.
#
# The supervisor in each session: checks the PR out into an isolated git worktree,
# fans out to the five angle reviewers, synthesizes one report with triage frontmatter,
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
REFRESH_META_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --max-parallel) MAX_PARALLEL="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --refresh-meta) REFRESH_META_ONLY=1; shift ;;  # rewrite metadata for all open PRs, no reviews
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "$DATA_DIR/reviews" "$DATA_DIR/meta"
[[ -f "$DATA_DIR/state.json" ]] || echo '{}' > "$DATA_DIR/state.json"

# The dashboard user's own GitHub login — excluded from "discussion since review" so your
# own comments don't flag as activity worth re-reading (you already know what you said).
ME="$(gh api user --jq .login 2>/dev/null || echo "")"

# Reap finished review sessions: a session cao-prr-<n> whose report file for the SHA it
# was launched on now exists has done its job — shut it down so it frees a parallelism slot.
# (Without this, idle-but-completed sessions hold slots and stall the queue.)
declare -A LAUNCH_SHA    # pr  -> sha it was launched to review
declare -A IDLE_SINCE    # pr  -> epoch when we first saw it idle w/o a report (0 = not idle)
declare -A NUDGES        # pr  -> how many watchdog nudges sent so far
WATCHDOG_SECS=420        # must be idle+reportless this long before we nudge
MAX_NUDGES=2             # give up (leave for manual/next run) after this many nudges

reap_finished() {
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^cao-prr-'); do
    local pr="${s#cao-prr-}"
    local sha="${LAUNCH_SHA[$pr]:-}"
    [[ -n "$sha" && -f "$DATA_DIR/reviews/${pr}-${sha}.md" ]] || continue
    cao shutdown --session "$s" >/dev/null 2>&1 && echo "  ✓ #$pr reviewed — reclaimed $s"
  done
}

# Watchdog for the race-stall: a supervisor can go idle after its final reviewer's message
# arrives without ever synthesizing (the message lands as the turn ends, so no turn fires to
# act on it — it parks holding all findings). We detect a session that has been idle (no tmux
# window activity) AND reportless for WATCHDOG_SECS, then prod it via `cao session send` to
# synthesize with whatever it holds. Call this periodically from the wait loops.
watchdog_nudge() {
  local now; now="$(date +%s)"
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^cao-prr-'); do
    local pr="${s#cao-prr-}"
    local sha="${LAUNCH_SHA[$pr]:-}"
    [[ -n "$sha" ]] || continue
    if [[ -f "$DATA_DIR/reviews/${pr}-${sha}.md" ]]; then IDLE_SINCE[$pr]=0; continue; fi
    # tmux activity flag: 1 => produced output since last check (actively working)
    local active; active="$(tmux list-windows -t "$s" -F '#{window_activity_flag}' 2>/dev/null | tr -d '\n')"
    if [[ "$active" == *1* ]]; then IDLE_SINCE[$pr]=0; continue; fi   # busy — reset idle timer
    local since="${IDLE_SINCE[$pr]:-0}"
    if [[ "$since" -eq 0 ]]; then IDLE_SINCE[$pr]="$now"; continue; fi
    (( now - since < WATCHDOG_SECS )) && continue                    # idle, but not long enough yet
    local n="${NUDGES[$pr]:-0}"
    [[ "$n" -ge "$MAX_NUDGES" ]] && continue
    NUDGES[$pr]=$((n+1)); IDLE_SINCE[$pr]="$now"                      # reset timer after nudging
    echo "  ⏰ watchdog: #$pr idle ${WATCHDOG_SECS}s w/o report — nudge $((n+1))/$MAX_NUDGES"
    cao session send "$s" \
      "Check your inbox now. If you hold findings from any reviewers, synthesize the report immediately with what you have (name any missing angle) and write it to ${DATA_DIR}/reviews/${pr}-${sha}.md with the YAML frontmatter (title, urgency, importance, verdict, summary), then remove the worktree. Do not wait for more reviewers." \
      --async >/dev/null 2>&1 || true
  done
}

# size bucket from total churn
size_bucket() {
  local n="$1"
  if   [[ "$n" -lt 10  ]]; then echo XS
  elif [[ "$n" -lt 50  ]]; then echo S
  elif [[ "$n" -lt 250 ]]; then echo M
  elif [[ "$n" -lt 800 ]]; then echo L
  else echo XL; fi
}

declare -A AUTHOR_MERGED   # login -> merged-PR count (cached per run)
author_merged_prs() {
  local login="$1"
  [[ -n "${AUTHOR_MERGED[$login]:-}" ]] && { echo "${AUTHOR_MERGED[$login]}"; return; }
  local c
  c="$(gh pr list --repo "$REPO" --state merged --author "$login" --json number --jq 'length' 2>/dev/null || echo 0)"
  AUTHOR_MERGED[$login]="$c"; echo "$c"
}

# Write the deterministic metadata file the dashboard renders as flag pills.
# Pulled straight from the PR JSON we already fetched — no diff read needed.
write_meta() {
  local pr="$1" sha="$2" json="$3"
  local title add del files created login labels ci rollup merged days
  title="$(jq -r '.title' <<<"$json")"
  add="$(jq -r '.additions' <<<"$json")"; del="$(jq -r '.deletions' <<<"$json")"
  files="$(jq -r '.changedFiles' <<<"$json")"
  created="$(jq -r '.createdAt' <<<"$json")"
  login="$(jq -r '.author.login' <<<"$json")"
  labels="$(jq -c '[.labels[].name]' <<<"$json")"
  # CI from statusCheckRollup conclusions
  rollup="$(jq -r '[.statusCheckRollup[]?.conclusion // .statusCheckRollup[]?.state] | @tsv' <<<"$json" 2>/dev/null || echo "")"
  if   [[ -z "$rollup" ]]; then ci=none
  elif grep -qiE 'FAILURE|ERROR|TIMED_OUT|CANCELLED' <<<"$rollup"; then ci=failing
  elif grep -qiE 'PENDING|IN_PROGRESS|QUEUED|EXPECTED' <<<"$rollup"; then ci=pending
  else ci=passing; fi
  merged="$(author_merged_prs "$login")"
  days="$(( ( $(date -u +%s) - $(date -u -d "$created" +%s) ) / 86400 ))"

  # --- Two distinct "since review" signals (only meaningful once a report exists) ---
  #   code_changed  : the PR head moved since we reviewed → needs RE-REVIEW (🔁)
  #   human_activity: a NON-BOT comment/review landed after our review → worth RE-READING (💬)
  # Bot accounts (codecov, dependabot, github-actions, copilot) are excluded so their
  # automated comments don't masquerade as human engagement.
  local act comments reviews code_changed human_activity rev_epoch
  act="$(gh pr view "$pr" --repo "$REPO" --json comments,reviews 2>/dev/null || echo '{}')"
  comments="$(jq -r '(.comments|length) // 0' <<<"$act" 2>/dev/null || echo 0)"
  reviews="$(jq -r '(.reviews|length) // 0' <<<"$act" 2>/dev/null || echo 0)"
  code_changed=false; human_activity=false

  # code_changed: a review exists at an OLDER sha but NOT at the current head sha.
  if [[ ! -f "$DATA_DIR/reviews/${pr}-${sha}.md" ]] && ls "$DATA_DIR/reviews/${pr}-"*.md >/dev/null 2>&1; then
    code_changed=true
  fi

  # human_activity: newest non-bot comment/review timestamp is after our review-file mtime.
  if [[ -f "$DATA_DIR/reviews/${pr}-${sha}.md" ]]; then
    rev_epoch="$(date -u -r "$DATA_DIR/reviews/${pr}-${sha}.md" +%s 2>/dev/null || echo 0)"
    # "others" = not a bot AND not the dashboard user (your own comments don't count).
    local latest_human
    latest_human="$(jq -r --arg me "$ME" '
      def is_bot($l): ($l|ascii_downcase) | test("bot|codecov|dependabot|github-actions|copilot");
      [ (.comments[]?, .reviews[]?)
        | (.author.login // "") as $l
        | select($l != "" and $l != $me and (is_bot($l)|not))
        | (.createdAt // .submittedAt // empty) ] | max // ""' <<<"$act" 2>/dev/null || echo "")"
    if [[ -n "$latest_human" ]]; then
      local h_epoch; h_epoch="$(date -u -d "$latest_human" +%s 2>/dev/null || echo 0)"
      (( h_epoch > rev_epoch )) && human_activity=true
    fi
  fi

  jq -n \
    --arg title "$title" --arg size "$(size_bucket $((add+del)))" \
    --argjson additions "$add" --argjson deletions "$del" --argjson files "$files" \
    --argjson days_waiting "$days" --arg author "$login" --argjson author_merged_prs "$merged" \
    --arg ci "$ci" --argjson labels "$labels" \
    --argjson comments "${comments:-0}" --argjson reviews "${reviews:-0}" \
    --argjson code_changed "$code_changed" --argjson human_activity "$human_activity" \
    '{title:$title,size:$size,additions:$additions,deletions:$deletions,files:$files,
      days_waiting:$days_waiting,author:$author,author_merged_prs:$author_merged_prs,
      ci:$ci,labels:$labels,comments:$comments,reviews:$reviews,
      code_changed:$code_changed,human_activity:$human_activity,draft:false}' \
    > "$DATA_DIR/meta/${pr}-${sha}.json"
}

echo "Discovering open PRs on $REPO …"
# non-draft PRs, newest first, capped at LIMIT. Fetch the full per-PR JSON so we can write
# metadata without extra calls; emit one compact JSON object per line.
# In refresh-meta mode, cover ALL open PRs (ignore LIMIT); otherwise cap at LIMIT.
disc_limit=$LIMIT
[[ "$REFRESH_META_ONLY" -eq 1 ]] && disc_limit=1000
mapfile -t PR_JSON < <(gh pr list --repo "$REPO" --state open \
  --json number,isDraft,headRefOid,title,additions,deletions,changedFiles,createdAt,author,labels,statusCheckRollup \
  --jq '[.[] | select(.isDraft|not)] | .[:'"$disc_limit"'][] | @json')
PRS=()
for j in "${PR_JSON[@]}"; do
  PRS+=("$(jq -r '"\(.number) \(.headRefOid)"' <<<"$j")")
done

# --refresh-meta: rewrite metadata for every open PR and exit (no reviews). Cheap way to
# correct stale CI/labels/size flags on the dashboard without spending review agents.
if [[ "$REFRESH_META_ONLY" -eq 1 ]]; then
  echo "Refreshing metadata for ${#PRS[@]} open PRs (no reviews)…"
  for i in "${!PRS[@]}"; do
    pr="${PRS[$i]%% *}"; sha="${PRS[$i]##* }"
    write_meta "$pr" "$sha" "${PR_JSON[$i]}" 2>/dev/null \
      && echo "  ✓ #$pr" || echo "  ✗ #$pr (metadata write failed)"
  done
  echo "Metadata refreshed. Reload the dashboard."
  exit 0
fi

echo "Will review ${#PRS[@]} PRs (limit $LIMIT, up to $MAX_PARALLEL at a time):"
printf '  #%s\n' "${PRS[@]%% *}"

launched=0
for i in "${!PRS[@]}"; do
  entry="${PRS[$i]}"
  pr="${entry%% *}"
  sha="${entry##* }"
  pr_json="${PR_JSON[$i]}"

  # write/refresh the metadata file so the dashboard has triage flags even before the
  # deep review lands (and for PRs whose review is skipped as already-current).
  write_meta "$pr" "$sha" "$pr_json" 2>/dev/null \
    || echo "    (metadata write for #$pr failed — dashboard flags will be partial)"

  # skip the deep review if already reviewed at this exact SHA (idempotent re-runs)
  if [[ -f "$DATA_DIR/reviews/${pr}-${sha}.md" ]]; then
    echo "  #$pr already reviewed at $sha — skipping (metadata refreshed)"
    continue
  fi

  # throttle: reap finished sessions + nudge any stalled ones, then wait while full
  while reap_finished; watchdog_nudge; [[ "$(tmux ls -F '#{session_name}' 2>/dev/null | grep -c '^cao-prr-')" -ge "$MAX_PARALLEL" ]]; do
    sleep 15
  done

  echo "  launching review session for #$pr (head $sha)…"
  # One session per PR. Two steps:
  #  1. `cao launch` starts the supervisor idle in its own session (prr-<n>).
  #  2. `cao session send --async` delivers the review task and returns immediately,
  #     so the driver can move on (subject to the MAX_PARALLEL throttle).
  # The supervisor runs in dashboard mode: checks out the PR in an isolated worktree,
  # fans out to the five reviewers, writes the report, then goes idle.
  msg="Review PR #$pr. MODE: dashboard, write report to ${DATA_DIR}/reviews/${pr}-${sha}.md"
  if cao launch --agents pr_review_supervisor --provider claude_code --yolo \
       --session-name "prr-${pr}" >/dev/null 2>&1; then
    sleep 12   # let the supervisor finish booting before sending the task
    LAUNCH_SHA[$pr]="$sha"
    cao session send "cao-prr-${pr}" "$msg" --async >/dev/null 2>&1 \
      || echo "    (send for #$pr failed — check 'cao session status cao-prr-${pr}')"
  else
    echo "    (launch for #$pr failed — check 'tmux ls')"
  fi
  launched=$((launched+1))
  sleep 4   # small stagger between PRs
done

echo "Launched $launched review session(s). Waiting for in-flight reviews to finish…"
# keep reaping + nudging stalled sessions until all produce a report (or we give up)
for _ in $(seq 1 80); do
  reap_finished
  watchdog_nudge
  [[ "$(tmux ls -F '#{session_name}' 2>/dev/null | grep -c '^cao-prr-')" -eq 0 ]] && break
  sleep 30
done
remaining="$(tmux ls -F '#{session_name}' 2>/dev/null | grep '^cao-prr-' || true)"
if [[ -n "$remaining" ]]; then
  echo "Still running (no report yet) — may be stalled; inspect with: tmux attach -t <name>"
  echo "$remaining" | sed 's/^/  /'
else
  echo "All reviews complete. Open the dashboard to triage."
fi
