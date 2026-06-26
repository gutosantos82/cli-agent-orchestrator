# PR Review: #325 — fix(kiro): detect kiro 2.8.x TUI idle state after MCP server init

## Summary

The PR fixes CAO's failure to detect the idle state of kiro-cli 2.8.x TUI sessions (which
broke after kiro replaced its input placeholder with a `agent · auto · ◔ N%` status bar),
extends the init guard in `get_status()`, and lifts two hard-coded 30s timeouts into
configurable constants (`TUI_INIT_TIMEOUT=90`, `SESSION_CREATE_TIMEOUT=120`). The root-cause
analysis is sound and the change is focused. **However, the new idle regex changes semantics
from "input field shown" to "always-present status bar", which risks premature COMPLETED
detection mid-response, and the timeouts are mutually incoherent on the fallback path.**
Combined with zero test coverage and a CI-breaking import line, this needs changes before
merge.

**Recommendation: Request changes.**

## Blocking (must fix before merge)

- **[correctness] kiro_cli.py:62/65 (`NEW_TUI_IDLE_PATTERN` / `_LOG`)** — The new alternative
  `· (?:auto|manual) · ◔` matches the kiro 2.8.x *status bar*, which is typically rendered
  **persistently — including while the agent is generating**. In `get_status()` Check 6,
  when `_input_received` is True and a new-TUI-idle line exists, it returns COMPLETED
  unconditionally → premature/truncated message extraction. It also defeats Check 2 ("Kiro
  is working"): the status bar sits *below* the ghost text, so `idle_after_working` becomes
  True and PROCESSING is no longer returned. Pre-2.8.x this branch matched the input
  *placeholder* (hidden during processing), so the semantics quietly changed. **Fix:** confirm
  `· auto · ◔` renders ONLY at idle; if it persists during processing, gate this indicator
  differently (e.g. require absence of the spinner %/ghost text, or only treat it as idle when
  no "working" marker follows it). This is the central risk.

- **[correctness] constants.py:188-190 + kiro_cli.py `TUI_INIT_TIMEOUT`** — Timeout
  incoherence. The `SESSION_CREATE_TIMEOUT` comment claims the server-side budget is "30s +
  10s shell wait" (40s), but this PR raised provider init to 90s. The non-yolo fallback path
  in `initialize()` runs init **twice**: `wait_until_status(90s)` → `/exit` →
  `wait_for_shell(10s)` → `wait_until_status(90s)` ≈ **190s+**. The client
  `SESSION_CREATE_TIMEOUT=120s` is **less** than this worst case, so the launch HTTP call
  will read-timeout client-side while the server is still legitimately initializing. (The
  single non-fallback path is fine: 90 < 120.) **Fix:** size `SESSION_CREATE_TIMEOUT` to cover
  `2×TUI_INIT_TIMEOUT` + shell waits (or derive it), and correct the stale "30s + 10s" comment.

- **[conventions] mcp_server/server.py:13** — Adding `SESSION_CREATE_TIMEOUT` to the
  single-line `from ...constants import API_BASE_URL, DEFAULT_PROVIDER, MCP_REQUEST_TIMEOUT,
  SESSION_CREATE_TIMEOUT` pushes the line to ~120 chars, over the black/isort limit of 100.
  `uv run black --check` / `isort --check-only` run in `ci.yml` (and every provider workflow)
  → **this will fail CI**. **Fix:** wrap into the parenthesized multi-line import form —
  `launch.py:10-17` already does exactly this for the same module.

- **[tests] No tests for the core behavioral changes** — The three behavioral changes (regex
  alternation, init-state guard, env-driven timeouts) ship with **zero regression tests**,
  in an area with a large existing unit suite (`test/providers/test_kiro_cli_unit.py`, ~1789
  lines). The regex change in particular can silently break IDLE/PROCESSING/COMPLETED
  detection across the whole TUI suite. Given the correctness risks above, manual testing on
  one kiro-cli build is not sufficient. **Fix (block on):** add 2.8.x positive **and negative**
  regex tests, an init-guard IDLE-vs-PROCESSING regression test (must not regress the issue
  #211 / `test_mcp_server_init_yields_processing` behavior), and env-var timeout
  parsing/override tests — all unit-level with `tmux_client` mocked.

## Important (should fix)

- **[correctness] kiro_cli.py:62 — hardcoded model slot** — The pattern hardcodes
  `(?:auto|manual)`, but the sibling `_new_tui_header_pattern` (line ~153) uses `.*` for that
  slot, implying it's variable. If a user pins a specific model the bar reads
  `saga · <model> · ◔ …` and will **not match** → idle never detected for 2.8.x → spurious
  legacy-ui fallback/timeout. The literal single spaces are also fragile vs the `\s+` the
  header pattern uses. **Fix:** mirror the header pattern, e.g. `·\s+\S+\s+·\s+◔`.

- **[correctness] kiro_cli.py — paste-drop suppression dropped for 2.8.x** — The PR comment
  notes kiro 2.8.x's new init line ("Initializing · type to queue a message") does NOT match
  `TUI_INITIALIZING_PATTERN`, deliberately leaving it unmatched and relying on the assumption
  that the old idle prompt only appears in the final render (~66 chars before true idle). If
  that assumption is wrong on any 2.8.x build/terminal width, `get_status()` returns
  IDLE/COMPLETED during the boot window and the first paste is silently dropped — the exact
  bug `TUI_INITIALIZING_PATTERN` exists to prevent. The added `new_tui_idle_after_init` check
  is benign but **moot** in 2.8.x (init_matches is empty there). **Fix:** add the 2.8.x init
  string to `TUI_INITIALIZING_PATTERN` rather than relying on the timing assumption.

- **[conventions] CHANGELOG.md** — The `[Unreleased]` section is empty; this PR adds no
  entry despite a user-facing behavior fix + two new operator env vars
  (`CAO_SESSION_CREATE_TIMEOUT`, `CAO_KIRO_TUI_INIT_TIMEOUT`). The project maintains per-PR
  CHANGELOG entries. **Fix:** add a `### Fixed` line under `[Unreleased]` referencing #325 and
  documenting the two env vars.

- **[conventions] env-var config placement** — `TUI_INIT_TIMEOUT` lives in
  `providers/kiro_cli.py` while its sibling `SESSION_CREATE_TIMEOUT` (added by this same PR)
  correctly goes in `constants.py`, which is the established home for `CAO_*` config. There is
  mixed precedent (server.py keeps `CAO_ENABLE_*` local), but a tunable timeout fits the
  constants.py group. **Fix:** move `CAO_KIRO_TUI_INIT_TIMEOUT` to constants.py, or justify the
  split.

- **[conventions] kiro_cli.py:89 — dead `# noqa: E501`** — flake8 is not configured anywhere
  in the repo (no config, not in dev deps, not in any workflow); CI enforces line length via
  black. The directive suppresses nothing and is misleading. Pre-existing, but the PR touches
  this region. **Fix:** drop the noqa (black leaves long regex/string literals alone).

## Nits (optional)

- **[security/correctness] unvalidated env-var parsing** — `int(os.environ.get("CAO_SESSION_CREATE_TIMEOUT","120"))`
  and `float(os.environ.get("CAO_KIRO_TUI_INIT_TIMEOUT","90"))` are evaluated at module import;
  a malformed value (`=abc`) raises ValueError and crashes import, and negative/zero values
  aren't rejected. **Risk is availability only** — these are operator/local env vars, not
  attacker- or agent-supplied, and the pattern matches existing env constants in the repo.
  Optional hardening: try/except with fallback + clamp to a positive minimum.

- **[correctness] no coupling between the two timeout knobs** — An operator raising
  `CAO_KIRO_TUI_INIT_TIMEOUT` above 120 without raising `CAO_SESSION_CREATE_TIMEOUT`
  reintroduces the client-side read timeout (see blocking #2). Worth a comment or runtime check.

- **[conventions] non-ASCII glyphs in regex** — `·` (U+00B7) and `◔` (U+25D4) are embedded
  literally. Consistent with existing literals in the file and valid UTF-8 (no bug). Note the
  file is internally inconsistent (line ~148 escapes `λ`). Optional: add a brief comment naming
  the glyphs since they read as invisible-intent in a regex.

## Tests

Coverage is **not adequate** — this is the strongest blocking signal alongside correctness.
No test files are in the diff. New tests belong in `test/providers/test_kiro_cli_unit.py`
(unmarked, runs in the fast unit set; mock `tmux_client` per existing
`@patch("cli_agent_orchestrator.providers.kiro_cli.tmux_client")` convention) — **not**
integration/e2e; no live CLI needed. Required coverage:

1. **Regex (high-risk):** positive match of `· auto · ◔` / `· manual · ◔` against the 2.8.x
   status bar, **and** negative tests proving it does NOT match agent prose containing
   "auto"/"manual"/a stray `◔`, nor a mid-response header redraw. Add a fixture mirroring the
   existing fixture style (`test/providers/fixtures/`).
2. **Init guard:** the 2.8.x indicator post-init resolves to IDLE (not stuck PROCESSING), while
   the pre-init "N of M mcp servers initialized" window still yields PROCESSING (don't regress
   issue #211).
3. **Timeouts:** assert `wait_until_status` is called with `timeout=90.0` by default and with a
   monkeypatched env override; same for `SESSION_CREATE_TIMEOUT` (note `test/mcp_server/test_assign.py`
   still asserts `MCP_REQUEST_TIMEOUT`).

> Note: both the tests and correctness reviewers reported they could not locate
> `SESSION_CREATE_TIMEOUT` in the working copy of `constants.py`/`launch.py`/`server.py` — this
> appears to be a stale checkout on the reviewer side; the PR diff clearly adds the constant and
> swaps the call sites. The diff is authoritative here. Worth a quick local confirm that the
> branch is fully applied before relying on the "constant missing" observation.

## Verdict

**Request changes** — two correctness blockers (persistent status-bar → premature COMPLETED;
timeout incoherence on the fallback path), one CI-breaking import line, and absent test
coverage for a regression-prone state machine.
