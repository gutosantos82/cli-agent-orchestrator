---
name: cao-pr-review
description: Review a GitHub pull request on the CLI Agent Orchestrator (CAO) repo. Use this skill whenever the user wants to review a CAO PR, asks to look over a pull request, gives a PR number or github.com/awslabs/cli-agent-orchestrator/pull/<n> URL, or wants feedback before merging a change to CAO. It layers CAO-specific checks (provider/plugin architecture, inclusive language, uv/black/isort/mypy/pytest conventions, security of tmux/subprocess/credential handling) on top of the built-in `/review` GitHub reviewer.
---

# CAO Pull Request Reviewer

Review a pull request on `cli-agent-orchestrator` (CAO). This skill builds on Claude
Code's built-in `/review` command — that handles fetching the PR, generic correctness
review, and posting inline comments. Your added value is the **CAO-specific lens**: this
repo has conventions and architecture that a generic reviewer won't know about.

> **Invoke explicitly for reliable triggering.** A short request like "review PR #312"
> often looks simple enough that Claude reviews it with `gh` directly and never loads this
> skill (measured recall on auto-triggering is low; precision is perfect, so it won't
> mis-fire). To guarantee the CAO-specific checks run, call it by name:
> `/cao-pr-review 312` (or pass a PR URL). Don't rely on the description to auto-trigger.

## Workflow

### 1. Identify the PR

The user may give a number (`#241`), a URL, or "the latest PR". Resolve it:

```bash
gh pr view <number-or-url> --json number,title,author,baseRefName,headRefName,files,additions,deletions,body
gh pr diff <number-or-url>
```

If no PR is specified, list open ones and ask which:

```bash
gh pr list --limit 20
```

> Note: `origin` is the fork (`gutosantos82/cli-agent-orchestrator`) and `upstream` is
> `awslabs/cli-agent-orchestrator`. Confirm which repo the PR lives on; pass `--repo` to
> `gh` if it's the upstream.

### 2. Run the generic pass

Invoke the built-in `/review` for the PR (correctness bugs, generic cleanups, inline
comments). Don't re-derive what it already covers — focus your own attention on the
CAO-specific checks below.

### 3. Run the CAO-specific pass

Read `references/cao-review-checklist.md` and apply it to the diff. The five dimensions
the user cares about most:

1. **CAO conventions** — inclusive language, commit/PR hygiene, project structure, the
   provider and plugin contracts, hardcoded-should-be-config, committed generated files.
2. **Correctness** — terminal status-detection logic, async/race conditions, async hygiene
   (blocking I/O on the event loop, un-timed network calls), read ops with hidden
   mutations, error paths.
3. **Security** — credential handling, command injection through tmux/subprocess, raw
   env/command overrides, GitHub Actions token permissions, file encoding/permissions, and
   `--yolo` / `--dangerously-skip-permissions` safety.
4. **Tests & coverage** — does the change ship tests, follow pytest markers, and not
   regress the suite?
5. **Consistency & drift** — doc/comment↔code drift, PR-description↔implementation
   mismatch, cross-provider interface consistency, dead/unused code, out-of-scope changes.
   This is the highest-volume category of real feedback on the CAO repo.

When a change touches `src/cli_agent_orchestrator/providers/`, the provider contract in
the existing `cao-provider` skill is the source of truth — read
`../cao-provider/SKILL.md` and check the diff against its File Checklist (a new provider
should touch the enum, manager, launch allowlist, tests, fixtures, docs, README, and
CHANGELOG).

### 4. Report

Group findings by **severity**, and within each by **dimension**. Use this structure:

```
# PR Review: #<n> — <title>

## Summary
<2-3 sentences: what the PR does, overall assessment, merge recommendation>

## Blocking (must fix before merge)
- **[security] <file>:<line>** — <finding + why it matters + suggested fix>

## Important (should fix)
- **[correctness] <file>:<line>** — ...

## Nits (optional)
- **[convention] <file>:<line>** — ...

## Tests
<Did the PR add/update tests? Do they cover the change? Any gaps?>

## Verdict
Approve / Approve with nits / Request changes — <one line>
```

Anchor every finding to `file:line` so it's clickable and easy to act on.

**For every finding, classify it as introduced or pre-existing.** Before you raise an
issue, check whether the PR actually created it or merely touched nearby code. If a risky
pattern (a blocking call, a private-attribute reach, a broad `except`) already existed and
the PR only extends or amplifies it, say so explicitly — "the PR amplifies rather than
introduces this" — and don't block on it. Conflating the two inflates the review and
erodes trust; a fix that's genuinely out of scope for this PR belongs in a follow-up note,
not the Blocking section.

### 5. Optionally post comments

Only if the user asks. Confirm the target repo first, then post:

```bash
gh pr comment <number> --repo <owner/repo> --body "<review markdown>"
```

For inline comments on specific lines, prefer letting the built-in `/review --comment`
handle placement, or use `gh api` to post a review with line anchors. Never post without
explicit confirmation — a public review comment is hard to walk back.

## Reference

- `references/cao-review-checklist.md` — the full dimension-by-dimension checklist.
- `../cao-provider/SKILL.md` — provider contract (read when the PR touches providers).
- `../cao-plugin/SKILL.md` — plugin/hook contract (read when the PR touches plugins/hooks).
