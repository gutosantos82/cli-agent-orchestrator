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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

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

# Fetch the repo's open GitHub code-scanning alerts into a single repo-level summary
# (pr-review-data/security.json) that the dashboard renders as a banner. Code scanning
# here only analyzes the default branch, so alerts are repo-global (not per-PR); we
# surface totals + severity breakdown + the worst offenders, with a link to the
# Security tab. Best-effort: needs the security_events scope; on failure we write an
# empty summary so the dashboard degrades gracefully.
write_security_summary() {
  local out="$DATA_DIR/security.json"
  local url="https://github.com/${REPO}/security/code-scanning"
  local alerts
  if ! alerts="$(gh api "repos/${REPO}/code-scanning/alerts?state=open&per_page=100" 2>/dev/null)"; then
    jq -n --arg url "$url" --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{generated_at:$t, url:$url, available:false, total_open:0, by_severity:{}, top_alerts:[]}' > "$out"
    echo "  ⚠ code-scanning alerts unavailable (need security_events scope?) — wrote empty security.json"
    return
  fi
  jq --arg url "$url" --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
    def sev: (.rule.security_severity_level // .rule.severity // "unknown");
    {
      generated_at: $t,
      url: $url,
      available: true,
      total_open: length,
      by_severity: (group_by(sev) | map({key: (.[0]|sev), value: length}) | from_entries),
      top_alerts: ( sort_by(
                      ({critical:0,high:1,medium:2,low:3,warning:4,note:5,unknown:6}[(.|sev)] // 9)
                    )
                    | .[:8]
                    | map({number, severity: (.|sev), rule: .rule.id,
                           path: .most_recent_instance.location.path,
                           line: .most_recent_instance.location.start_line,
                           url: .html_url}) )
    }' <<<"$alerts" > "$out"
  echo "  🔒 security summary: $(jq -r '.total_open' "$out") open code-scanning alert(s) → security.json"
}

# Reap finished review sessions: a session cao-prr-<n> whose report file for the SHA it
# was launched on now exists has done its job — shut it down so it frees a parallelism slot.
# (Without this, idle-but-completed sessions hold slots and stall the queue.)
declare -A LAUNCH_SHA    # pr  -> sha it was launched to review
declare -A IDLE_SINCE    # pr  -> epoch when we first saw it idle w/o a report (0 = not idle)
declare -A NUDGES        # pr  -> how many watchdog nudges sent so far
declare -A PANE_FP       # pr  -> last content fingerprint of the supervisor pane
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
# act on it — it parks holding all findings). We detect a session that has made no progress
# AND is reportless for WATCHDOG_SECS, then prod it via `cao session send` to synthesize with
# whatever it holds. Call this periodically from the wait loops.
#
# Progress is measured by fingerprinting the supervisor pane's content between checks, NOT by
# tmux's window_activity_flag: that flag only clears when a human *views* the window, so a
# parked supervisor kept reading as "active" and never got nudged (the original bug). We strip
# the volatile chrome first — the status bar's ticking "Midway:" timer, the box rules, and the
# prompt line — which change on their own even when the agent is idle. A live agent still moves
# Deliver a message to a review session's supervisor. Prefer `cao session send`
# (tracked, structured); if that fails — e.g. the terminal reads as status
# 'unknown' so cao-server refuses delivery — fall back to typing the task straight
# into the supervisor pane (:0), which the kiro TUI accepts regardless of the
# detected status. Keeps task delivery resilient to status-detection glitches.
deliver_task() {
  local sess="$1" text="$2"
  cao session send "$sess" "$text" --async >/dev/null 2>&1 && return 0
  if tmux has-session -t "$sess" 2>/dev/null; then
    tmux send-keys -t "${sess}:0" "$text"; sleep 1; tmux send-keys -t "${sess}:0" Enter
    echo "    ($sess: cao send failed — delivered via direct pane input)"
    return 0
  fi
  return 1
}

# the fingerprint (new transcript lines, or a ticking spinner); a parked one is static.
watchdog_nudge() {
  local now; now="$(date +%s)"
  for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^cao-prr-'); do
    local pr="${s#cao-prr-}"
    local sha="${LAUNCH_SHA[$pr]:-}"
    [[ -n "$sha" ]] || continue
    if [[ -f "$DATA_DIR/reviews/${pr}-${sha}.md" ]]; then IDLE_SINCE[$pr]=0; continue; fi
    # Fingerprint the supervisor pane (window 0), ignoring self-ticking chrome.
    local fp; fp="$(tmux capture-pane -t "${s}:0" -p 2>/dev/null \
      | grep -vaE 'Midway:|bypass permissions|───|❯' | md5sum | cut -d' ' -f1)"
    if [[ "$fp" != "${PANE_FP[$pr]:-}" ]]; then                     # content moved — still working
      PANE_FP[$pr]="$fp"; IDLE_SINCE[$pr]=0; continue
    fi
    local since="${IDLE_SINCE[$pr]:-0}"
    if [[ "$since" -eq 0 ]]; then IDLE_SINCE[$pr]="$now"; continue; fi
    (( now - since < WATCHDOG_SECS )) && continue                    # idle, but not long enough yet
    local n="${NUDGES[$pr]:-0}"
    [[ "$n" -ge "$MAX_NUDGES" ]] && continue
    NUDGES[$pr]=$((n+1)); IDLE_SINCE[$pr]="$now"                      # reset timer after nudging
    echo "  ⏰ watchdog: #$pr idle ${WATCHDOG_SECS}s w/o report — nudge $((n+1))/$MAX_NUDGES"
    deliver_task "$s" \
      "Check your inbox now. If you hold findings from any reviewers, synthesize the report immediately with what you have (name any missing angle) and write it to ${DATA_DIR}/reviews/${pr}-${sha}.md with the YAML frontmatter (title, urgency, importance, verdict, summary), then remove the worktree. Do not wait for more reviewers." \
      || true
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
  act="$(gh pr view "$pr" --repo "$REPO" --json comments,reviews,mergeable,reviewDecision,reviewRequests 2>/dev/null || echo '{}')"
  comments="$(jq -r '(.comments|length) // 0' <<<"$act" 2>/dev/null || echo 0)"
  reviews="$(jq -r '(.reviews|length) // 0' <<<"$act" 2>/dev/null || echo 0)"
  code_changed=false; human_activity=false

  # --- Human-engagement + mergeability signals (surfaced on the dashboard) ---
  #   mergeable        : MERGEABLE / CONFLICTING / UNKNOWN  (CONFLICTING = clashes with base)
  #   review_decision  : GitHub's rollup — APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED
  #   human_reviewers  : deduped non-bot reviewers -> their LATEST review state
  #   last_human       : latest non-bot comment/review note (author + snippet)
  # These let the dashboard warn when a human is already reviewing, a human decision
  # disagrees with our verdict, or the PR no longer merges cleanly.
  local mergeable review_decision human_reviewers last_human
  mergeable="$(jq -r '.mergeable // "UNKNOWN"' <<<"$act" 2>/dev/null || echo UNKNOWN)"
  review_decision="$(jq -r '.reviewDecision // ""' <<<"$act" 2>/dev/null || echo "")"
  human_reviewers="$(jq -c '
    def is_bot($l): ($l|ascii_downcase)|test("bot|codecov|dependabot|github-actions|copilot");
    [ .reviews[]? | {login:(.author.login//""), state:.state, at:.submittedAt}
      | select(.login!="" and (is_bot(.login)|not)) ]
    | group_by(.login) | map(max_by(.at)) | map({login, state})' <<<"$act" 2>/dev/null || echo '[]')"
  [[ -n "$human_reviewers" ]] || human_reviewers='[]'
  last_human="$(jq -c '
    def is_bot($l): ($l|ascii_downcase)|test("bot|codecov|dependabot|github-actions|copilot");
    [ (.comments[]?, .reviews[]?) | {login:(.author.login//""), body:(.body//""), at:(.createdAt//.submittedAt)}
      | select(.login!="" and (is_bot(.login)|not) and ((.body//"")|length)>0) ]
    | (sort_by(.at) | last) // null
    | if .==null then null else {login, at, note:((.body|gsub("[\r\n]+";" "))[:200])} end' <<<"$act" 2>/dev/null || echo 'null')"
  [[ -n "$last_human" ]] || last_human='null'

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
    --arg mergeable "$mergeable" --arg review_decision "$review_decision" \
    --argjson human_reviewers "$human_reviewers" --argjson last_human "$last_human" \
    '{title:$title,size:$size,additions:$additions,deletions:$deletions,files:$files,
      days_waiting:$days_waiting,author:$author,author_merged_prs:$author_merged_prs,
      ci:$ci,labels:$labels,comments:$comments,reviews:$reviews,
      code_changed:$code_changed,human_activity:$human_activity,
      mergeable:$mergeable,review_decision:$review_decision,
      human_reviewers:$human_reviewers,last_human:$last_human,draft:false}' \
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
  write_security_summary
  echo "Metadata refreshed. Reload the dashboard."
  exit 0
fi

echo "Will review ${#PRS[@]} PRs (limit $LIMIT, up to $MAX_PARALLEL at a time):"
printf '  #%s\n' "${PRS[@]%% *}"
write_security_summary

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
  if cao launch --agents pr_review_supervisor --provider kiro_cli --yolo --headless \
       --session-name "prr-${pr}" >/dev/null 2>&1; then
    sleep 12   # let the supervisor finish booting before sending the task
    LAUNCH_SHA[$pr]="$sha"
    deliver_task "cao-prr-${pr}" "$msg" \
      || echo "    (delivery for #$pr failed — check 'tmux ls')"
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

# --- self-learning: distill this batch's outcomes into lessons -------------------
# Each supervisor reports one outcome per PR (Step 7 of its profile). Retrospection
# runs ONCE per batch, not per PR, so lessons are drawn across the whole run.
# Opt-in: only when learning is on AND at least one review was launched. The
# retrospector reads outcomes via list_outcomes and writes worker-scoped lessons;
# it never touches GitHub. Instruction promotion stays a separate manual step
# (`cao memory promote <agent>` — dry-run by default).
if [[ "$launched" -gt 0 ]] && [[ "${CAO_PRR_RETROSPECT:-1}" = "1" ]]; then
  if curl -sf -m 5 "http://127.0.0.1:9889/outcomes" >/dev/null 2>&1; then
    echo "Dispatching retrospector for this batch…"
    # `retrospector` is a BUILT-IN profile: it appears in `cao profile list` but
    # `cao launch` does NOT materialize the provider-side agent JSON for built-ins.
    # Without ~/.kiro/agents/retrospector.json, kiro-cli logs "no agent with name
    # retrospector found. Falling back to user specified default" and renders a
    # prompt with no "[profile]" prefix — so CAO's profile-derived idle pattern
    # never matches, the terminal never reaches IDLE, and create_terminal fails
    # after provider_init_timeout (60s). Install it first; idempotent and cheap.
    if [[ ! -f "$HOME/.kiro/agents/retrospector.json" ]]; then
      retro_md="$REPO_ROOT/src/cli_agent_orchestrator/agent_store/retrospector.md"
      if [[ -f "$retro_md" ]]; then
        echo "  installing built-in retrospector profile for kiro_cli…"
        cao install "$retro_md" >/dev/null 2>&1 \
          || echo "  (retrospector install failed — launch will likely time out)"
      else
        echo "  (built-in retrospector.md not found at $retro_md)"
      fi
    fi
    # Capture launch output: swallowing it is what hid the real failure for a week.
    retro_log="$DATA_DIR/retro-launch.log"
    if cao launch --agents retrospector --provider kiro_cli --yolo --headless \
         --session-name "prr-retro" >"$retro_log" 2>&1; then
      sleep 12
      deliver_task "cao-prr-retro" \
        "Retrospect on workflow pr-review — the review batch that just finished. Agents involved: pr_review_supervisor, correctness_reviewer, security_reviewer, tests_reviewer, conventions_reviewer, consistency_reviewer, conversation_reviewer, vision_reviewer, verifier. Read the recorded outcomes, store only lessons that would change what an agent does next time, then reply with your one-line summary." \
        || echo "  (retrospector delivery failed — inspect: tmux attach -t cao-prr-retro)"
      # Give it room to read outcomes and store lessons, then leave the session for
      # inspection if it hasn't exited on its own.
      for _ in $(seq 1 20); do
        tmux has-session -t cao-prr-retro 2>/dev/null || break
        sleep 15
      done
      if tmux has-session -t cao-prr-retro 2>/dev/null; then
        echo "  (retrospector still running — inspect: tmux attach -t cao-prr-retro)"
      else
        echo "  retrospection complete — review lessons with: cao memory list"
      fi
    else
      echo "  (retrospector launch failed — skipping retrospection; see $retro_log)"
      sed 's/^/    | /' "$retro_log" 2>/dev/null | tail -5
    fi
  else
    echo "Learning disabled (/outcomes unavailable) — skipping retrospection."
  fi
fi
