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
- **Permission bypass**: changes touching `--yolo` / `--dangerously-skip-permissions` /
  `--auto-approve` or `PROVIDERS_REQUIRING_WORKSPACE_ACCESS` deserve extra scrutiny — they
  widen what a spawned agent can do. Confirm the widening is intentional and documented.
- **Path traversal**: agent-store and profile resolution should not allow `../` escapes
  out of the intended directory.

## 4. Tests & coverage

- New behavior ships with tests. New provider → unit tests in
  `test/providers/test_<provider>_unit.py` plus fixtures in `test/providers/fixtures/`.
- Markers are correct: integration tests marked `integration`, e2e under `test/e2e/`
  marked `e2e`, async tests `asyncio`. The fast unit run is
  `uv run pytest test/ --ignore=test/e2e -m "not integration"`.
- Don't require a live CLI in unit tests — `tmux_client` should be mocked with
  `unittest.mock.patch`.
- Note coverage gaps for the changed lines specifically, not just overall.

## 5. CI expectations

The PR will face these gates — call out anything that will obviously fail:
- Unit tests on the Python 3.10 / 3.11 / 3.12 matrix.
- Code quality: `black --check`, `isort --check-only`, `mypy src/`.
- Trivy security scan (CRITICAL/HIGH) and dependency review on new deps.
- Provider-specific path-triggered workflows (e.g. editing `providers/codex.py` triggers
  `test-codex-provider.yml`).

> Q CLI is slated for deprecation — flag new development built around it; prefer Kiro CLI,
> Claude Code, or Codex CLI.
