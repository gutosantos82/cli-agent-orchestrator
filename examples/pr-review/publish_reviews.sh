#!/usr/bin/env bash
# Publish CAO pr-review verdicts to GitHub with an APPROVE guard.
#
# Why: `Request changes` / comments are low blast-radius (feedback the author can
# weigh). An `approve` is you vouching, under your name, that a PR on a PUBLIC repo
# is merge-ready — and it counts toward reviewDecision=APPROVED. So this script will
# REFUSE to auto-approve a PR whose diff touches sensitive paths or is large, unless
# you explicitly ack it (having actually read it). request-changes/comments post freely.
#
# Verdict comes from each report's frontmatter (verdict: Approve|Approve with nits|
# Request changes|<comment>). You still pass the explicit PR list — this never scans
# and posts on its own (preserves the human-confirmation rule).
#
# Usage:
#   publish_reviews.sh [--repo R] [--dry-run] [--ack "PR..."] \
#                      [--max-lines N] [--max-files N] PR [PR...]
#
#   --dry-run   print the action per PR, post nothing
#   --ack "N M" PRs you have read and consciously allow to auto-approve despite the guard
#   --max-lines additions+deletions above which an approve is gated (default 400)
#   --max-files changed files above which an approve is gated (default 15)
#
# Env overrides: CAO_PRR_SENSITIVE_RE (extended regex over changed file paths).
set -uo pipefail

REPO="awslabs/cli-agent-orchestrator"
DRY_RUN=0
AS_COMMENT=0
ACTION_OVERRIDE=""
ACK_LIST=" "
MAX_LINES="${CAO_PRR_MAX_LINES:-400}"
MAX_FILES="${CAO_PRR_MAX_FILES:-15}"

# Paths that make an approve "needs-human". Tuned for this repo: provider status
# detection, auth/credentials/security, tool-permission surface, and release/CI.
SENSITIVE_RE="${CAO_PRR_SENSITIVE_RE:-src/cli_agent_orchestrator/providers/|auth|cred|secret|token|oauth|security|midway|permission|allowed_?tools|yolo|(^|/)\.github/|pyproject\.toml|uv\.lock|RELEASING|constants\.py|mcp_server/server\.py}"

args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)      REPO="$2"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --as-comment) AS_COMMENT=1; shift ;;
    --action)    ACTION_OVERRIDE="$2"; shift 2 ;;
    --ack)       ACK_LIST=" $2 "; shift 2 ;;
    --max-lines) MAX_LINES="$2"; shift 2 ;;
    --max-files) MAX_FILES="$2"; shift 2 ;;
    -* ) echo "unknown option: $1" >&2; exit 2 ;;
    * )  args+=("$1"); shift ;;
  esac
done
[[ ${#args[@]} -gt 0 ]] || { echo "usage: publish_reviews.sh [opts] PR [PR...]" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
DATA_DIR="pr-review-data"
[[ -f "$DATA_DIR/state.json" ]] || echo '{}' > "$DATA_DIR/state.json"

strip_fm(){ awk 'BEGIN{c=0} /^---[[:space:]]*$/{c++;next} c>=2{print}' "$1"; }
# Drop internal, dashboard-only sections so they never reach the public PR comment.
# The posted comment and the dashboard view are intentionally DIFFERENT: the dashboard
# shows the full report (triage context), the comment shows only what's useful to the PR
# author/maintainers. Removes any level-2 (## ) section whose heading is dashboard-only
# (operator notes, "prior feedback / already raised" restatements, publish-guard notes),
# from that heading up to the next level-2 heading or EOF. Kept in the report file itself.
strip_human_notes(){ awk '
  /^##[[:space:]]/{
    if (tolower($0) ~ /notes? for the human|human publisher|publisher note|do not post|internal[ -]only|reviewer note|prior feedback|already raised|publish[ -]?guard|roadmap|vision fit/) { skip=1; next }
    skip=0
  }
  !skip { print }
'; }
# Backstop: drop any line that references internal review tooling / operator
# triage so it can never reach a public PR review, even if the model slips it
# into the body instead of the (stripped) Notes section. High-precision tokens
# only — these never legitimately belong in a code review comment.
scrub_operator(){ grep -viE 'needs_human|--ack|publish[ _-]?guard|publish_reviews\.sh|dashboard will hold|for the ack|re-review before posting|a human should (skim|decide|look|read|review)'; }
verdict_of(){ awk -F': ' '/^verdict:/{gsub(/"/,"",$2); print $2; exit}' "$1"; }

acted_any=0
for pr in "${args[@]}"; do
  head="$(gh pr view "$pr" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null)"
  f="$DATA_DIR/reviews/${pr}-${head}.md"
  if [[ -z "$head" ]]; then echo "#$pr SKIP: could not resolve head"; continue; fi
  # Stale-head guard: only ever post a report that matches the CURRENT head.
  if [[ ! -f "$f" ]]; then
    echo "#$pr SKIP: no report at current head ${head:0:7} — re-review before posting"; continue
  fi

  verdict="$(verdict_of "$f")"
  case "$verdict" in
    *"Request changes"*) action=request-changes; act=requested ;;
    *Approve*)           action=approve;         act=approved  ;;
    *)                   action=comment;         act=commented ;;
  esac
  # --as-comment: post the review body as a plain comment regardless of verdict
  # (used by the Telegram "Comment" button — no approval, no guard).
  if [[ "$AS_COMMENT" -eq 1 ]]; then action=comment; act=commented; fi
  # --action: explicit human-chosen action (dashboard/Telegram click == consent).
  # Stale-head + notes-stripping still apply; approve bypasses the hold since the
  # click IS the ack.
  if [[ -n "$ACTION_OVERRIDE" ]]; then
    case "$ACTION_OVERRIDE" in
      approve) action=approve;         act=approved;  ACK_LIST="$ACK_LIST$pr " ;;
      request) action=request-changes; act=requested ;;
      comment) action=comment;         act=commented ;;
      *) echo "#$pr SKIP: bad --action '$ACTION_OVERRIDE'"; continue ;;
    esac
  fi

  # --- APPROVE guard -------------------------------------------------------
  if [[ "$action" == approve ]]; then
    files="$(gh pr view "$pr" --repo "$REPO" --json files --jq '.files[].path' 2>/dev/null)"
    read -r adds dels nfiles < <(gh pr view "$pr" --repo "$REPO" \
      --json additions,deletions,changedFiles --jq '"\(.additions) \(.deletions) \(.changedFiles)"' 2>/dev/null)
    lines=$(( ${adds:-0} + ${dels:-0} ))
    hit="$(echo "$files" | grep -Ei "$SENSITIVE_RE" | head -5 | paste -sd, - 2>/dev/null || true)"
    nh="$(awk -F': ' '/^needs_human:/{gsub(/[ "]/,"",$2);print $2;exit}' "$f")"
    reason=""
    [[ "$nh" == "true" ]] && reason="report flagged needs_human"
    [[ -n "$hit" ]] && reason="${reason:+$reason; }sensitive paths: $hit"
    if (( lines > MAX_LINES )) || (( ${nfiles:-0} > MAX_FILES )); then
      reason="${reason:+$reason; }large diff (${lines} lines, ${nfiles:-?} files)"
    fi
    if [[ -n "$reason" ]] && [[ "$ACK_LIST" != *" $pr "* ]]; then
      echo "#$pr HOLD: approve gated — $reason"
      echo "        Read the diff, then re-run with: --ack \"$pr\"  (or downgrade to a comment)"
      continue
    fi
    [[ "$ACK_LIST" == *" $pr "* ]] && echo "#$pr approve ACKed by operator"
  fi

  # --- Post ---------------------------------------------------------------
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "#$pr [dry-run] would $action at ${head:0:7} (verdict: ${verdict:-none})"
    continue
  fi

  strip_fm "$f" | strip_human_notes | scrub_operator > "/tmp/rev_${pr}.md"
  # Branch on gh's EXIT STATUS, never on captured output.
  #
  # This used to be `posted=$(gh ... && echo ok || echo fail)`, which conflates
  # the command's stdout with the sentinel. `gh pr comment` prints the new
  # comment's URL to stdout, so $posted became "<url>\nok" and the `== ok` test
  # FAILED on a SUCCESSFUL post: the script reported "FAILED to post", skipped
  # the state.json update, and invited a retry that posted a SECOND copy. That
  # produced a duplicate comment on #521 (deleted manually) and again on #547.
  # `gh pr review` happens to write its URL to stderr, so the review path looked
  # fine and hid the bug — which is exactly why this must not depend on which
  # stream a given gh subcommand chooses.
  #
  # Keep gh's own output visible (it goes to our stdout/stderr as usual) and let
  # `if` read the exit status directly.
  if [[ "$action" == comment ]]; then
    gh pr comment "$pr" --repo "$REPO" --body-file "/tmp/rev_${pr}.md"
  else
    gh pr review "$pr" --repo "$REPO" "--$action" --body-file "/tmp/rev_${pr}.md"
  fi && posted=ok || posted=fail
  if [[ "$posted" == ok ]]; then
    echo "#$pr -> POSTED ($act) at ${head:0:7}"
    jq --arg p "$pr" --arg a "$act" --arg s "$head" --arg t "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" \
       '.[$p]=((.[$p]//{})+{acted:$a,acted_sha:$s,acted_at:$t})' \
       "$DATA_DIR/state.json" > /tmp/st.json && mv /tmp/st.json "$DATA_DIR/state.json"
    acted_any=1
  else
    echo "#$pr -> FAILED to post"
  fi
done

# Verify what actually landed on GitHub
if [[ "$DRY_RUN" -eq 0 && "$acted_any" -eq 1 ]]; then
  me="$(gh api user --jq .login 2>/dev/null)"
  echo "--- verify (latest review state by $me) ---"
  for pr in "${args[@]}"; do
    gh api "repos/$REPO/pulls/$pr/reviews" \
      --jq '[.[]|select(.user.login=="'"$me"'")]|last|"#'"$pr"' -> \(.state)"' 2>/dev/null || true
  done
fi
