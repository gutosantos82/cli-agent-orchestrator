#!/usr/bin/env bash
# Flow gating script for the CAO PR-review pipeline.
#
# Contract (see docs/flows.md + services/flow_service.py):
#   - Must finish within 30s (subprocess timeout).
#   - Must print EXACTLY one JSON object on stdout: {"execute": bool, "output": {...}}.
#   - All diagnostics MUST go to stderr / the driver log, never stdout.
#
# Design (Option A — gate fires the driver, never uses the flow's agent):
#   1. Fast-classify open non-draft PRs: a PR needs review if there is no
#      report at pr-review-data/reviews/<pr>-<HEAD_SHA>.md (NEW or head moved).
#   2. If nothing needs review  -> emit {"execute": false} and do nothing.
#   3. If something needs review -> clean stale cao-prr-* sessions and launch
#      run_reviews.sh --limit 20 DETACHED (setsid+nohup), then emit
#      {"execute": false}. The driver runs independently and clears the lock.
#
# Phase 3 (publishing to GitHub) is intentionally NOT automated — a human
# reviews the generated reports and posts verdicts manually.
#
# A single-holder lock (pr-review-data/.driver.lock) prevents overlapping
# driver runs; a stale lock older than STALE_LOCK_MIN minutes is reclaimed.
set -euo pipefail

REPO="${CAO_PRR_REPO:-awslabs/cli-agent-orchestrator}"
LIMIT="${CAO_PRR_LIMIT:-20}"
STALE_LOCK_MIN="${CAO_PRR_STALE_LOCK_MIN:-90}"

# Resolve repo root from this script's location (examples/pr-review/) so the
# gate works regardless of the cwd cao-server invokes it from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

DATA_DIR="pr-review-data"
LOCKDIR="$DATA_DIR/.driver.lock"
DRIVER_LOG="$DATA_DIR/driver.log"
mkdir -p "$DATA_DIR/reviews"

# Helper: emit the JSON verdict on stdout and exit 0. Only ever called once.
emit() { printf '{"execute": false, "output": {}}\n'; exit 0; }

log() { echo "[pr-review-gate $(date -u +%H:%M:%S)] $*" >&2; }

# --- Reclaim a stale lock (previous driver crashed without releasing) --------
if [ -d "$LOCKDIR" ]; then
  if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +"$STALE_LOCK_MIN" 2>/dev/null)" ]; then
    log "reclaiming stale lock (> ${STALE_LOCK_MIN}m old)"
    rm -rf "$LOCKDIR"
  else
    log "driver already running (lock held) — skipping"
    emit
  fi
fi

# --- Fast classify: any open non-draft PR without a current-head report? -----
open="$(gh pr list --repo "$REPO" --state open --json number,headRefOid,isDraft \
        --jq '.[]|select(.isDraft|not)|"\(.number) \(.headRefOid)"' 2>/dev/null || true)"

needed=""
while read -r pr sha; do
  [ -z "${pr:-}" ] && continue
  [ -f "$DATA_DIR/reviews/${pr}-${sha}.md" ] || needed="$needed $pr"
done <<< "$open"
needed="${needed# }"

if [ -z "$needed" ]; then
  log "no NEW/RE-REVIEW PRs — nothing to do"
  emit
fi

log "PRs needing review:$([ -n "$needed" ] && echo " $needed")"

# --- Acquire lock (atomic mkdir) --------------------------------------------
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  log "lost lock race — another gate is firing; skipping"
  emit
fi

# --- Clean stale review sessions, then fire the detached runner --------------
# pr-review-run.sh does: notify start -> run driver -> notify outcomes -> release lock.
for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^cao-prr-' || true); do
  cao shutdown --session "$s" >/dev/null 2>&1 || true
  tmux kill-session -t "$s" 2>/dev/null || true
done

log "firing driver for:$([ -n "$needed" ] && echo " $needed") (detached) — log: $DRIVER_LOG"
setsid nohup "$SCRIPT_DIR/pr-review-run.sh" $needed >> "$DRIVER_LOG" 2>&1 &
disown 2>/dev/null || true

emit
