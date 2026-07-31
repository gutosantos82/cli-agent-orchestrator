#!/usr/bin/env bash
# Flow gating script for the CAO PR-review pipeline.
#
# Contract (see docs/flows.md + services/flow_service.py):
#   - Must finish within 30s (subprocess timeout).
#   - Must print EXACTLY one JSON object on stdout: {"execute": bool, "output": {...}}.
#   - All diagnostics MUST go to stderr / the driver log, never stdout.
#   - CRITICAL: no surviving child may hold stdout/stderr open. flow_service runs
#     us via `subprocess.run(..., capture_output=True, timeout=30)`, which does not
#     return until the captured PIPES CLOSE — not merely until we exit. A detached
#     child that inherits either stream keeps its pipe open, so the flow blocks the
#     full 30s and is killed with TimeoutExpired even though this script exited in
#     ~2s. That killed the 03:00 and 07:00 runs on 2026-07-31, each time AFTER the
#     lock was taken, orphaning .driver.lock so the retry a minute later skipped.
#     Every background command below therefore redirects BOTH streams away from
#     the inherited pipes (and the driver additionally gets its own session).
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
  # Liveness check FIRST, age second. The lock records the driver pid that owns it
  # (owner.pid); if that process is gone the lock is dead no matter how young it
  # is, so reclaim immediately. This is the durable half of the timeout fix: when
  # the flow kills us on its 30s cap it uses SIGKILL, which bash CANNOT trap, so
  # the EXIT trap below does not run and the lock survives. Age alone then made
  # every subsequent tick skip for STALE_LOCK_MIN (90m) — on 2026-07-31 the 03:00
  # kill suppressed the 03:02 retry, and the 07:00 kill left a 6-hour dead lock.
  owner_pid="$(cat "$LOCKDIR/owner.pid" 2>/dev/null || true)"
  if [ -n "$owner_pid" ] && ! kill -0 "$owner_pid" 2>/dev/null; then
    log "reclaiming lock — owner pid $owner_pid is gone"
    rm -rf "$LOCKDIR"
  elif [ -z "$owner_pid" ] \
       && [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then
    # No owner recorded: either a pre-upgrade lock or one killed between mkdir and
    # the stamp. Give it a 2-minute grace period, then treat it as dead.
    log "reclaiming unstamped lock (> 2m old, no owner recorded)"
    rm -rf "$LOCKDIR"
  elif [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +"$STALE_LOCK_MIN" 2>/dev/null)" ]; then
    log "reclaiming stale lock (> ${STALE_LOCK_MIN}m old)"
    rm -rf "$LOCKDIR"
  else
    log "driver already running (lock held by pid ${owner_pid:-?}) — skipping"
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
# If we die between here and handing the lock to the driver (e.g. the flow kills
# us on timeout), release it rather than orphaning it for STALE_LOCK_MIN minutes.
# That orphaning is what made the 03:00 failure suppress the 03:02 retry too.
# Cleared once the driver is successfully spawned — from then on the lock is the
# driver's to release (pr-review-run.sh removes it on exit).
trap 'rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT INT TERM
# Stamp OUR pid while we hold it. If we are SIGKILLed (untrappable) the next tick
# sees a pid that no longer exists and reclaims the lock in seconds instead of
# waiting out STALE_LOCK_MIN. Replaced with the driver's pid once it is spawned.
echo $$ > "$LOCKDIR/owner.pid" 2>/dev/null || true

# --- Clean stale review sessions, then fire the detached runner --------------
# pr-review-run.sh does: notify start -> run driver -> notify outcomes -> release lock.
# Each helper is time-bounded and fully redirected: `cao shutdown` talks to
# cao-server over HTTP and has been observed taking ~2s per live session, so an
# unbounded loop over several stalled sessions could alone approach the 30s cap.
for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^cao-prr-' || true); do
  timeout 10 cao shutdown --session "$s" >/dev/null 2>&1 < /dev/null || true
  timeout 5 tmux kill-session -t "$s" >/dev/null 2>&1 < /dev/null || true
done

log "firing driver for:$([ -n "$needed" ] && echo " $needed") (detached) — log: $DRIVER_LOG"
# Detach FULLY: stdout+stderr to the driver log AND stdin from /dev/null, so the
# child holds none of the pipes flow_service is capturing. Without `< /dev/null`
# the child inherits our stdin; without redirecting BOTH output streams it keeps
# a capture pipe open and blocks subprocess.run for its whole lifetime (see the
# contract note at the top). setsid also puts it in a new session so it survives
# the gate exiting.
setsid nohup "$SCRIPT_DIR/pr-review-run.sh" $needed >> "$DRIVER_LOG" 2>&1 < /dev/null &
driver_pid=$!
disown 2>/dev/null || true

# Hand the lock to the driver: record ITS pid so a later tick can tell a running
# driver from a dead one, then disarm our trap so exiting does not delete a lock
# the driver now owns (pr-review-run.sh removes it when it finishes).
echo "$driver_pid" > "$LOCKDIR/owner.pid" 2>/dev/null || true
trap - EXIT INT TERM

emit
