#!/usr/bin/env bash
# Change-driven test selection for the CAO pr-review verifier.
#
# Goal: complement CI, don't replicate it. CI already runs the full suite + lint/
# format/coverage. Locally we want the *targeted* tests for what the PR changed —
# and especially provider tests, since provider status/idle/prompt detection is
# fixture-driven here and is the thing CI mocks.
#
# Given the PR's changed files (from --files, else `git diff` in the worktree), this
# maps changed src paths to the mirrored test/ targets, flags which providers are
# touched and whether each provider's CLI is installed (for a Tier-2 live smoke),
# and separates out CI-only changes (docs/CI/test-only) that need no local run.
#
# Usage:
#   select_tests.sh [--worktree DIR] [--base REF] [--files "a b c"]
# Output: human-readable + machine lines (TEST_TARGETS=, PROVIDERS=, CI_ONLY=).
set -uo pipefail

WT="."; BASE="origin/main"; FILES_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --worktree) WT="$2"; shift 2 ;;
    --base)     BASE="$2"; shift 2 ;;
    --files)    FILES_OVERRIDE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# --- changed files ----------------------------------------------------------
if [[ -n "$FILES_OVERRIDE" ]]; then
  changed="$(printf '%s\n' $FILES_OVERRIDE)"
else
  changed="$(git -C "$WT" diff --name-only "${BASE}...HEAD" 2>/dev/null)"
  [[ -z "$changed" ]] && changed="$(git -C "$WT" diff --name-only HEAD~1 2>/dev/null)"
fi

SRC_PREFIX="src/cli_agent_orchestrator/"
declare -A TARGETS=()      # test targets (dir or file) that exist
declare -A PROVIDERS=()    # provider module -> 1
ci_only=()

# provider module -> candidate CLI binary (for Tier-2 live smoke)
provider_bin() {
  case "$1" in
    kiro_cli) echo "kiro-cli q" ;; claude_code) echo "claude" ;;
    codex) echo "codex" ;; antigravity_cli) echo "antigravity" ;;
    hermes) echo "hermes" ;; kimi_cli) echo "kimi" ;;
    copilot_cli) echo "copilot" ;; opencode_cli) echo "opencode" ;;
    cursor_cli) echo "cursor" ;; *) echo "" ;;
  esac
}

test_target_for() {  # map a src subpath to a mirrored test path that exists
  local sub="$1" cand
  # try file-level mirror first (foo/bar.py -> test/foo/test_bar*.py), then dir
  local dir="${sub%/*}" base="$(basename "$sub" .py)"
  for cand in "test/${dir}/test_${base}.py" "test/${dir}"; do
    [[ -e "$WT/$cand" ]] && { echo "$cand"; return; }
  done
  # walk up to the nearest existing test dir
  while [[ "$dir" == */* ]]; do dir="${dir%/*}"; [[ -d "$WT/test/$dir" ]] && { echo "test/$dir"; return; }; done
  [[ -d "$WT/test/$dir" ]] && echo "test/$dir"
}

while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  case "$f" in
    "$SRC_PREFIX"*)
      sub="${f#$SRC_PREFIX}"
      # provider module?
      if [[ "$sub" =~ ^providers/([a-z0-9_]+)\.py$ ]]; then
        PROVIDERS["${BASH_REMATCH[1]}"]=1
      fi
      t="$(test_target_for "$sub")"; [[ -n "$t" ]] && TARGETS["$t"]=1
      ;;
    test/*|*.md|docs/*|.github/*|pyproject.toml|uv.lock|*.cfg|*.toml|*.txt|*.yml|*.yaml)
      ci_only+=("$f") ;;
    *) ci_only+=("$f") ;;   # scripts, examples, misc — CI/other, no mirrored unit test
  esac
done <<< "$changed"

echo "=== change-driven test selection (base: $BASE) ==="
echo "src changes -> ${#TARGETS[@]} test target(s); providers touched: ${#PROVIDERS[@]}"

# providers first (highest value)
prov_line=""
for p in "${!PROVIDERS[@]}"; do
  inst="MISSING"; for b in $(provider_bin "$p"); do command -v "$b" >/dev/null 2>&1 && { inst="installed:$b"; break; }; done
  prov_line="$prov_line ${p}(cli:${inst})"
  # always include the provider suites that exercise status/prompt detection via fixtures
  for t in "test/providers/test_${p}_cli_unit.py" "test/providers"; do [[ -e "$WT/$t" ]] && TARGETS["$t"]=1; done
done
[[ -n "$prov_line" ]] && TARGETS["test/providers/test_permission_prompt_detection.py"]=1

echo "PROVIDERS=${prov_line# }"
echo "TEST_TARGETS=$(printf '%s ' "${!TARGETS[@]}" | sort -u | tr '\n' ' ')"
echo "CI_ONLY=${ci_only[*]:-}"

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "RECOMMENDATION: no mirrored src tests changed — likely docs/CI/test-only. CI covers this; skip local run (note it, don't fabricate)."
else
  echo "RECOMMENDATION: in Docker, run: uv run pytest ${!TARGETS[*]} -m 'not e2e' -q  (add 'and not integration' unless doing a provider live-smoke)."
  [[ -n "$prov_line" ]] && echo "  PROVIDER FOCUS: drive the changed provider's status/idle/prompt detection against test/providers/fixtures/*; for any cli:installed provider also attempt a Tier-2 live smoke; cli:MISSING -> fixture/unit only + report NOT VERIFIED for live behavior."
fi
