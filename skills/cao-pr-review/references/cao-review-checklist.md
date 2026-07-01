# CAO PR Review Checklist

Apply this against the PR diff. Each section maps to a review dimension. Not every item
applies to every PR — skip what's irrelevant and concentrate where the diff actually
lands. Explain *why* a finding matters; don't just cite a rule.

## 1. CAO conventions

### Inclusive language
This repo follows the Amazon inclusive-language policy. Flag any of these in new code,
comments, identifiers, or docs and suggest the replacement:

| Don't use | Use instead |
|---|---|
| master | primary, main, leader, controller |
| slave | replica, secondary, follower, responder |
| whitelist | allowlist, approved list, inclusion list |
| blacklist | denylist, blocklist, exclusion list |

(`main` branch and `git` remotes are fine — this is about new terminology the PR adds.)

### Commit & PR hygiene
- PR targets `main` and is focused on one change (CONTRIBUTING.md asks contributors not to
  reformat unrelated code — flag large incidental reformatting that obscures the real diff).
- Commit messages are clear. Merged history uses Conventional Commits
  (`feat:`, `fix:`, `docs:`, `build(deps):`, with optional scope like `fix(kiro_cli):`).
  Note if the PR's commits diverge wildly from that.
- User-facing changes update `CHANGELOG.md`.
- New deps were added via `uv add` (so `pyproject.toml` **and** `uv.lock` both change). A
  `pyproject.toml` dependency change with no `uv.lock` update is a red flag.

### Project structure
Code lives under `src/cli_agent_orchestrator/` in the expected package:
`api/`, `cli/`, `clients/`, `mcp_server/`, `models/`, `providers/`, `services/`, `utils/`.
Tests mirror that layout under `test/`. Flag files that landed in the wrong place.

### Hardcoded values that should be configurable
Roles, paths, timeouts, or poll counts baked into code that users would reasonably want to
set. Real example: hard-coded `data_analyst` / `analysis_supervisor` / `report_generator`
roles — "users should be able to define their roles." Suggest a config key or parameter.

### Committed generated artifacts
Generated files that shouldn't be tracked (`coverage*.json`, build output, caches). Real
example: `coverage.json` / `coverage2.json` committed. Suggest removal + a `.gitignore` entry.

## 2. Correctness

### Provider terminal logic (if `providers/` touched)
This is the highest-risk area in CAO — status is inferred by parsing terminal output.
- **Status-detection order** must be: WAITING_USER_ANSWER → COMPLETED → IDLE → PROCESSING
  → ERROR. A reordering can hang handoffs or misreport completion.
- **Stale buffer**: status checks must look at *recent* lines, not the whole scrollback,
  or a past response marker triggers a false COMPLETED.
- **Alt-screen vs scrollback**: full-screen TUIs need different detection than inline
  output — verify the right path is used for the CLI in question.
- **Regex** is defined at module level (not rebuilt per call) and ANSI codes are stripped
  before matching and before extracting the final response text.

### General
- **Introduced vs. pre-existing**: for each correctness concern, determine whether the PR
  created it or inherited it. A blocking `subprocess.run` on the async event loop, for
  instance, may already exist in a sibling method — if the PR only extends that pattern,
  flag it as amplified-not-introduced and route it to a follow-up, not a blocker.
- Async code: no unawaited coroutines, no shared mutable state across concurrent
  assign/handoff without a lock (per-directory lock conflicts are a known failure mode).
- **Async hygiene** (frequent real bug here): no synchronous I/O on the event loop — a sync
  file write, DB call, or `subprocess.run` inside an `async def` should use
  `asyncio.to_thread`; and network calls need a `timeout=` (`requests.get(...)` with no
  timeout stalls the whole loop if the peer hangs).
- **Read op with hidden mutation**: a function named like a read (`get_*`, `list_*`,
  `check_*`) that deletes or writes as a side effect. Real example: `get_session()` deleting
  DB records. The mutation should be renamed or split out.
- Error paths: timeouts (shell warm-up, CLI launch), subprocess failures, and missing
  files are handled, not swallowed.
- Edge cases the tests should cover: empty terminal output, Unicode, very long output,
  multiple responses in one buffer.

## 3. Security

- **Credentials**: no AWS keys, tokens, or secrets logged, written to disk unencrypted,
  or committed. Watch `~/.aws/cli-agent-orchestrator/` config writes.
- **Command injection**: anything interpolated into a tmux `send-keys`, shell command, or
  `subprocess` call must be validated/escaped. User- or agent-supplied strings
  (agent names, prompts, file paths) reaching a shell are the prime suspects.
- **Raw env-var / command overrides**: an env var whose value is passed unescaped to a
  shell or used as a command. Real example: `CAO_COPILOT_COMMAND` passed raw. Require
  validation (must start with the expected binary) or removal.
- **Permission bypass**: changes touching `--yolo` / `--dangerously-skip-permissions` /
  `--auto-approve` or `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` deserve extra scrutiny — they
  widen what a spawned agent can do. Confirm the widening is intentional and documented.
- **Path traversal**: agent-store and profile resolution should not allow `../` escapes
  out of the intended directory. (CodeQL's top real finding on this repo is "uncontrolled
  data used in a path expression" — a user/agent value flowing into a filesystem path.)
- **GitHub Actions token permissions**: a workflow (`.github/workflows/*.yml`) with no
  scoped `permissions:` grants the `GITHUB_TOKEN` broad write. Suggest least-privilege.
- **File encoding / permissions**: prompt/config files written with the platform-default
  encoding or default permissions can leak or corrupt. Prefer explicit `encoding="utf-8"`
  and restrictive modes for anything under `~/.aws/cli-agent-orchestrator/`.

## 4. Tests & coverage

- New behavior ships with tests. New provider → unit tests in
  `test/providers/test_<provider>_unit.py` plus fixtures in `test/providers/fixtures/`.
- Markers are correct: integration tests marked `integration`, e2e under `test/e2e/`
  marked `e2e`, async tests `asyncio`. The fast unit run is
  `uv run pytest test/ --ignore=test/e2e -m "not integration"`.
- Don't require a live CLI in unit tests — `tmux_client` should be mocked with
  `unittest.mock.patch`.
- Note coverage gaps for the changed lines specifically, not just overall.

## 5. Consistency & drift

The highest-volume category of real review feedback on the CAO repo. Code that contradicts
its own docs, comments, PR description, or the patterns the rest of the codebase follows.

- **Doc / comment ↔ code drift**: a docstring, comment, `CODEBASE.md`, `README.md`, or
  `docs/*.md` referencing a symbol/field/behavior the diff renamed or removed. When a
  comment or doc names a function, grep the tree to confirm it still exists. Real example:
  "CODEBASE.md still references `check_and_send_pending_messages()`, removed in favor of
  `deliver_pending()`."
- **PR-description ↔ implementation mismatch**: the PR body claims a value, file, or
  behavior the diff doesn't deliver. Real example: "PR says `extraction_tail_lines=5000`
  but the code hard-codes 2000."
- **Cross-provider interface consistency** (`providers/*.py`): every provider follows the
  same contract. `get_status()` must stay **read-only** (a provider that sends keystrokes
  from it is wrong); I/O goes through the `tmux_client` abstraction, not raw
  `subprocess.run`; startup uses `wait_until_status()` rather than a fixed poll count.
- **Dead / unused code**: patterns, constants, params, or imports added or left that are
  never used. Real example: "`BUSY_PATTERN` / `SPINNER_PATTERN` defined but not used."
- **Out-of-scope changes**: large incidental reformatting or unrelated edits in a focused
  PR — flag as a **nit** and suggest a separate PR.

## 6. CI expectations

The PR will face these gates — call out anything that will obviously fail:
- Unit tests on the Python 3.10 / 3.11 / 3.12 matrix.
- Code quality: `black --check`, `isort --check-only`, `mypy src/`.
- Trivy security scan (CRITICAL/HIGH) and dependency review on new deps.
- Provider-specific path-triggered workflows (e.g. editing `providers/codex.py` triggers
  `test-codex-provider.yml`).

> Q CLI is slated for deprecation — flag new development built around it; prefer Kiro CLI,
> Claude Code, or Codex CLI.

## Few-shot examples (real accepted findings on this repo)

These are actual review comments that landed on merged CAO PRs. Match this **claim → why →
concrete fix**, single-paragraph, identifier-quoting style. Each is a good finding.

- **[correctness / async]** `services/log_writer.py` — "This is a sync file write inside an
  async loop; every output will block the event loop briefly." → wrap in `asyncio.to_thread`.
- **[correctness / read-mutates]** `services/session_service.py` — "`get_session()` is a
  read operation that unexpectedly deletes DB records as a side effect. Should be a separate
  cleanup function or at minimum renamed to make the mutation explicit."
- **[correctness / concurrency]** `services/cleanup_service.py` — "The outer session holds
  terminal ORM objects while `delete_terminal` opens a separate `SessionLocal()`. With
  SQLite this can cause 'database is locked'. Fix: collect IDs first, close the session,
  then delete in a separate loop."
- **[security]** `providers/copilot_cli.py` — "`CAO_COPILOT_COMMAND` env var value passed
  raw to shell without validation or escaping. At minimum validate it starts with
  `copilot`, or remove the override entirely."
- **[consistency / cross-provider]** `providers/copilot_cli.py` — "`get_status()` sends
  keystrokes (`Y`, `1`, Enter). Every other provider treats `get_status()` as read-only.
  Creates race conditions when multiple callers poll."
- **[consistency / drift]** `CODEBASE.md` — "This data-flow doc still references
  `inbox_service.check_and_send_pending_messages()`, but that function was removed in favor
  of `deliver_pending()`."
- **[consistency / dead code]** `providers/copilot_cli.py` — "`BUSY_PATTERN` is defined but
  not used, do we need it?"
- **[conventions / hardcode]** `providers/copilot_cli.py` — "why are we hard-coding the
  `data_analyst` role here? users should be able to define their roles."
- **[conventions / generated file]** `coverage.json` — "generated files (~95KB) that should
  not be tracked. Please remove them and add `coverage*.json` to `.gitignore`."
- **[tests]** `test/providers/test_q_cli_integration.py` — "The fixture creates temp dirs in
  `$HOME`. If the test runner is killed before the yield cleanup runs, these `.cao_test_tmp_*`
  dirs will leak." → use `tmp_path` or register cleanup that survives SIGKILL where possible.
