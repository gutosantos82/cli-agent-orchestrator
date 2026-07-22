#!/usr/bin/env bash
# Build the PR relationship graph (graph.json) for the dashboard /graph view.
# Thin wrapper around build_pr_graph.py (stdlib + gh only). Run from the repo root.
#
#   examples/pr-review/build_pr_graph.sh                 # defaults (newest 150 PRs, files for 120)
#   examples/pr-review/build_pr_graph.sh --limit 200 --file-window 150
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/build_pr_graph.py" "$@"
