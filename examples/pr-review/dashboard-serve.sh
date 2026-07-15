#!/usr/bin/env bash
# Serve the PR-review dashboard (read-only) on PORT (default 8787).
#
# Runs server.py in an EPHEMERAL uv env with only its actual deps (--no-project),
# to avoid a full CAO project sync (transitive numpy fails to build on this host's
# old GCC). server.py is standalone (no cli_agent_orchestrator import).
#
# Read-only by default: --execute is NOT passed, so the dashboard renders reviews
# but cannot post to GitHub (publishing stays in publish_reviews.sh).
#
# Usage: dashboard-serve.sh [PORT]
set -euo pipefail
PORT="${1:-8787}"
EXEC=""
if [ "${2:-}" = "--execute" ] || [ "${CAO_PRR_DASHBOARD_EXECUTE:-}" = "1" ]; then
  EXEC="--execute"   # in-page action buttons post via the guarded publish_reviews.sh
fi
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
exec uv run --no-project \
  --with fastapi --with 'uvicorn[standard]' --with markdown --with pyyaml \
  python examples/pr-review/dashboard/server.py --data-dir pr-review-data --port "$PORT" $EXEC
