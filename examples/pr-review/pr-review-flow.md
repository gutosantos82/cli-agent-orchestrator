---
name: pr-review
schedule: "0 23,3,7 * * *"   # 09:00/13:00/17:00 AEST (UTC+10) = 23:00/03:00/07:00 UTC. Daily: weekend fires are cheap no-ops. NOTE: cron is UTC; if you observe AEDT (UTC+11, ~Oct–Apr) shift these -1h.
agent_profile: pr_review_manager
provider: kiro_cli
script: ./pr-review-gate.sh
---

The PR-review driver is fired directly by the gate script (`pr-review-gate.sh`),
which always returns `execute: false`, so this flow never actually launches the
`pr_review_manager` agent — the `agent_profile`/`provider` fields are only here
because the flow schema requires them.

What the scheduled run does:
1. The gate fast-classifies open non-draft PRs on awslabs/cli-agent-orchestrator.
2. If any PR has no report at its current head (NEW or head moved), it cleans
   stale `cao-prr-*` sessions and launches `run_reviews.sh --limit 20` detached
   (reviewers run on kiro_cli / claude-opus-4.8, so no interactive Midway prompt).
3. Reports land in `pr-review-data/reviews/<pr>-<head>.md`; the driver log is
   `pr-review-data/driver.log`.

Publishing to GitHub (approve / request-changes) is deliberately NOT automated —
review the generated reports and post verdicts manually.
