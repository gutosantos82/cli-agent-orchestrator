#!/usr/bin/env python3
"""Standard PR-review status table for interactive (Claude/agent) sessions.

WHY THIS EXISTS
The dashboard has rich triage tags, but ad-hoc `gh` queries in a chat session kept
reporting a thinner, subtly misleading picture. Three recurring mistakes this
script exists to prevent:

  1. Reporting only PR *age*. A 90-day-old PR touched yesterday is healthy; a
     20-day-old PR whose author went silent 20 days ago is stalled. Age alone
     cannot tell them apart, so we always report open-age AND idle/silence.
  2. Counting the PR author's own replies, or bots, as "someone reviewed this."
     Author self-replies and Copilot/codecov comments are not maintainer review.
  3. Guessing who counts as a maintainer from names. Maintainer status is read
     from the repo collaborator API (admin/maintain/write => maintainer), so
     outside contributors are never mistaken for maintainers.

Definitions (kept explicit so every report means the same thing):
  maintainer   collaborator with admin, maintain, or write/push role
  ball         who owes the next move: US if the author moved last, else AUTHOR
  silent       days since the AUTHOR last pushed/commented/reviewed
  last actor   who made the most recent move, and in which role
  others       maintainers other than you and the author who have engaged
  nudge        a maintainer comment made AFTER the last maintainer review
                (i.e. a follow-up ping, not the review itself)

Usage:
  pr_report.py                      # all reviewed PRs, grouped by who is blocked
  pr_report.py --unacted            # only PRs still awaiting our verdict
  pr_report.py --stalled            # only PRs where the author has gone quiet
  pr_report.py --pr 470 --pr 521    # specific PRs
  pr_report.py --json               # machine-readable, for further processing
  pr_report.py --me <login>         # whose reviews count as "ours" (default: gh auth user)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

REPO = "awslabs/cli-agent-orchestrator"
DASHBOARD = "http://127.0.0.1:8787/api/prs"

# Accounts that comment/review but are never a human signal.
BOTS = {
    "copilot-pull-request-reviewer",
    "copilot-swe-agent",
    "copilot",
    "github-actions",
    "github-advanced-security",
    "codecov-commenter",
    "codecov",
    "sonarcloud",
    "dependabot",
}
MAINTAINER_ROLES = {"admin", "maintain", "write", "push"}


def sh(args: list[str], timeout: int = 120) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}…: {out.stderr.strip()[:200]}")
    return out.stdout


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def days_since(ts: str | None) -> int | None:
    return None if not ts else (now() - parse(ts)).days


def load_maintainers() -> set[str]:
    """Maintainers per the collaborator API — never inferred from names."""
    raw = sh(["gh", "api", f"repos/{REPO}/collaborators?per_page=100",
              "--jq", '.[] | "\\(.login)\\t\\(.role_name)"'])
    out = set()
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        login, role = line.split("\t", 1)
        if role.strip().lower() in MAINTAINER_ROLES:
            out.add(login.strip())
    return out


def load_dashboard() -> dict[str, dict]:
    """Our own review verdicts, keyed by PR. Empty if the dashboard is down."""
    try:
        import urllib.request
        with urllib.request.urlopen(DASHBOARD, timeout=25) as r:
            return {str(p["pr"]): p for p in json.load(r)}
    except Exception:
        return {}


def classify(pr: str, me: str, maintainers: set[str], card: dict) -> dict:
    d = json.loads(sh([
        "gh", "pr", "view", pr, "--repo", REPO, "--json",
        "number,title,author,createdAt,isDraft,mergeable,reviewDecision,state,"
        "additions,deletions,changedFiles,commits,reviews,comments,statusCheckRollup",
    ]))
    author = d["author"]["login"]

    # Split every human event into author-side vs other-side. Bots excluded
    # entirely: they are not review signal (mistake #2 above).
    author_ev: list[tuple[str, str]] = []
    other_ev: list[tuple[str, str, str]] = []  # (ts, login, kind)
    reviews_by_other: list[tuple[str, str, str]] = []

    for c in d.get("commits", []):
        if c.get("committedDate"):
            author_ev.append((c["committedDate"], "commit"))
    for v in d.get("reviews", []):
        who = (v.get("author") or {}).get("login")
        ts = v.get("submittedAt")
        if not who or not ts or who in BOTS:
            continue
        if who == author:
            author_ev.append((ts, "review"))
        else:
            other_ev.append((ts, who, f"review:{v.get('state')}"))
            reviews_by_other.append((ts, who, v.get("state") or ""))
    # Track @mentions of `me` and whether I have said anything SINCE the last one.
    # An unanswered direct ping is invisible in every other column: #495 sat with a
    # maintainer's "friendly ping" for 7 days while its row looked like ordinary
    # "waiting on author", because the ping is a comment by someone else and does
    # not change author-silence, idle, or verdict state.
    # (ts, who pinged, comment kind, comment id) — the id lets us check whether the
    # ping was acknowledged with a REACTION rather than a reply.
    mentions: list[tuple[str, str, str, int | None]] = []
    my_comments: list[str] = []
    for c in d.get("comments", []):
        who = (c.get("author") or {}).get("login")
        ts = c.get("createdAt")
        if not who or not ts or who in BOTS:
            continue
        body = c.get("body") or ""
        if who == me:
            my_comments.append(ts)
        elif f"@{me}" in body:
            # `gh pr view --json comments` gives no numeric id; recover it from the
            # comment url (…/#issuecomment-<id>) so we can query its reactions.
            cid = None
            url = c.get("url") or ""
            if "issuecomment-" in url:
                try:
                    cid = int(url.rsplit("issuecomment-", 1)[1])
                except ValueError:
                    cid = None
            mentions.append((ts, who, "issue", cid))
        if who == author:
            author_ev.append((ts, "comment"))
        else:
            other_ev.append((ts, who, "comment"))
    # Review-thread (inline) comments are a separate REST collection that
    # `gh pr view --json comments` does not return, and pings land there too.
    try:
        inline = json.loads(sh(["gh", "api",
                                f"repos/{REPO}/pulls/{pr}/comments", "--paginate"], timeout=60) or "[]")
    except Exception:  # noqa: BLE001 — inline pings are a bonus signal, never fatal
        inline = []
    for c in inline:
        who = ((c.get("user") or {}).get("login")) or ""
        ts = c.get("created_at")
        if not who or not ts or who in BOTS:
            continue
        if who == me:
            my_comments.append(ts)
        elif f"@{me}" in (c.get("body") or ""):
            mentions.append((ts, who, "review", c.get("id")))
    mentions.sort()
    last_mention = mentions[-1] if mentions else None
    # Unanswered only if I have posted NOTHING after the most recent ping. My own
    # reviews count as a response too (a re-review answers a "please re-look" ping).
    my_latest = max(my_comments + [t for t, w, _ in other_ev if w == me], default=None)
    mention_unanswered = bool(last_mention and (not my_latest or my_latest < last_mention[0]))
    # A REACTION on the ping counts as an acknowledgement. Sometimes there is nothing
    # to answer — the ping is purely informational ("the ball's back on your side") —
    # and a 👍 is the honest, low-noise reply. Without this the row nags forever and
    # the operator learns to ignore the flag, which costs us the real ones.
    mention_ack_reaction: str | None = None
    if mention_unanswered and last_mention and last_mention[3]:
        kind, cid = last_mention[2], last_mention[3]
        path = ("issues/comments" if kind == "issue" else "pulls/comments")
        try:
            reactions = json.loads(sh(
                ["gh", "api", f"repos/{REPO}/{path}/{cid}/reactions"], timeout=45) or "[]")
            mine_r = [r for r in reactions
                      if ((r.get("user") or {}).get("login")) == me]
            if mine_r:
                mention_ack_reaction = mine_r[-1].get("content")
                mention_unanswered = False
        except Exception:  # noqa: BLE001 — absent reactions must never fail the row
            pass

    author_ev.sort()
    other_ev.sort()
    last_author = author_ev[-1] if author_ev else None
    last_other = other_ev[-1] if other_ev else None

    # Ball: US when the author moved most recently (or nobody has ever replied).
    ours = bool(last_author and (not last_other or last_author[0] > last_other[0]))
    if not last_author and not last_other:
        ours = True

    # Last actor + the role that makes it meaningful.
    if last_other and (not last_author or last_other[0] > last_author[0]):
        actor, actor_ts, actor_kind = last_other[1], last_other[0], last_other[2]
        actor_role = ("you" if actor == me
                      else "maintainer" if actor in maintainers else "contributor")
    elif last_author:
        actor, actor_ts, actor_kind = author, last_author[0], last_author[1]
        actor_role = "author"
    else:
        actor = actor_ts = actor_kind = actor_role = None

    # Other maintainers engaged (excluding you and the author) — the bus-factor signal.
    others = sorted({w for _, w, _ in other_ev
                     if w != me and w != author and w in maintainers})
    others_reviewed = sorted({w for _, w, s in reviews_by_other
                              if w != me and w != author and w in maintainers and s})
    # Nudge: a maintainer comment AFTER the last maintainer review.
    last_review_ts = max((t for t, w, _ in reviews_by_other if w in maintainers),
                         default=None)
    nudges = [t for t, w, k in other_ev
              if k == "comment" and w in maintainers and (not last_review_ts or t > last_review_ts)]

    sc = d.get("statusCheckRollup") or []
    failed = [c for c in sc
              if (c.get("conclusion") or c.get("state")) in
              ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT")]
    ci = "none" if not sc else ("failing" if failed else "passing")

    return {
        "pr": str(d["number"]),
        "title": d["title"],
        "author": author,
        "author_is_maintainer": author in maintainers,
        "open_days": days_since(d["createdAt"]),
        "author_silent_days": days_since(last_author[0]) if last_author else None,
        "idle_days": days_since(max([e[0] for e in author_ev] +
                                    [e[0] for e in other_ev])) if (author_ev or other_ev) else None,
        "ball": "US" if ours else "AUTHOR",
        "last_actor": actor,
        "last_actor_role": actor_role,
        "last_actor_kind": actor_kind,
        "last_actor_days": days_since(actor_ts) if actor_ts else None,
        "other_maintainers": others,
        "other_maintainers_reviewed": others_reviewed,
        "sole_reviewer": not others_reviewed,
        "nudges": len(nudges),
        "mentions": len(mentions),
        "mention_unanswered": mention_unanswered,
        "mention_ack_reaction": mention_ack_reaction,
        "last_mention_by": last_mention[1] if last_mention else None,
        "last_mention_days": days_since(last_mention[0]) if last_mention else None,
        "last_nudge_days": days_since(max(nudges)) if nudges else None,
        "draft": d["isDraft"],
        "state": d["state"],
        # GitHub reports mergeable=UNKNOWN once a PR is closed/merged, and also
        # while it lazily recomputes mergeability for an open PR. Show the terminal
        # state for closed/merged; render an open UNKNOWN as "?" rather than
        # implying we know it is conflict-free.
        "mergeable": (d["state"] if d["state"] != "OPEN"
                      else ("?" if d["mergeable"] == "UNKNOWN" else d["mergeable"])),
        "review_decision": d["reviewDecision"],
        "ci": ci,
        "size": (d["additions"] or 0) + (d["deletions"] or 0),
        "files": d["changedFiles"],
        "our_verdict": card.get("verdict"),
        "our_acted": card.get("acted"),
        "verdict_stale": bool(card.get("stale")),
    }


def bucket(r: dict) -> tuple[int, str]:
    if r.get("state") and r["state"] != "OPEN":
        return (5, "CLOSED / MERGED — no action")
    if r["draft"]:
        return (4, "DRAFT — not actionable")
    # An unanswered direct @mention outranks everything else that is still open: a
    # human explicitly asked YOU a question and has had no reply. This is its own
    # group because such a PR can otherwise sit in "waiting on author" and look
    # like nobody's move (observed: #495, pinged 2026-07-29, unanswered 7 days).
    if r.get("mention_unanswered"):
        return (0, "@YOU MENTIONED, NOT ANSWERED — a human is waiting on your reply")
    if r["ball"] == "US":
        return (1, "BALL IN OUR COURT — author is waiting on us")
    if (r["author_silent_days"] or 0) >= 14:
        return (3, "STALLED ON AUTHOR — feedback delivered, author quiet >=14d")
    return (2, "WAITING ON AUTHOR — recent, normal")


def render(rows: list[dict], me: str) -> None:
    rows = sorted(rows, key=lambda r: (bucket(r)[0],
                                       -(r["author_silent_days"] or 0)))
    cur = None
    for r in rows:
        b, label = bucket(r)
        if b != cur:
            print(f"\n── {label} ──")
            print(f"{'PR':>5} {'author':<16}{'open':>5}{'silent':>7}{'idle':>6}  "
                  f"{'last move (who/role)':<30}{'others':<22}"
                  f"{'our verdict':<18}{'acted':<10}{'CI':<8}{'merge':<12}flags")
            print("-" * 152)
            cur = b
        others = (",".join(r["other_maintainers"])[:20] or "—")
        flags = []
        if r.get("mention_unanswered"):
            flags.append(f"@YOU-UNANSWERED({r['last_mention_by']},{r['last_mention_days']}d)")
        elif r.get("mention_ack_reaction"):
            # Acknowledged by reaction, not a reply — shown so it reads as a deliberate
            # ack rather than the ping having silently vanished from the report.
            flags.append(f"@you-ack:{r['mention_ack_reaction']}")
        if r["verdict_stale"]:
            flags.append("STALE-VERDICT")
        if r["sole_reviewer"]:
            flags.append("SOLE-REVIEWER")
        if r["nudges"]:
            flags.append(f"nudged×{r['nudges']}@{r['last_nudge_days']}d")
        if r["mergeable"] == "CONFLICTING":
            flags.append("CONFLICTS")
        if r["author_is_maintainer"]:
            flags.append("author=maintainer")
        last = f"{r['last_actor'] or '—'}/{r['last_actor_role'] or '—'}"
        print(f"{('#'+r['pr']):>5} {r['author'][:15]:<16}"
              f"{str(r['open_days'])+'d':>5}{str(r['author_silent_days'])+'d':>7}"
              f"{str(r['idle_days'])+'d':>6}  {last[:28]:<30}{others:<22}"
              f"{(r['our_verdict'] or '—')[:16]:<18}{(r['our_acted'] or '—'):<10}"
              f"{r['ci'][:6]:<8}{r['mergeable'][:10]:<12}{' '.join(flags)}")
    print(f"\n{len(rows)} PR(s). 'you' = {me}. maintainer = repo collaborator "
          f"(admin/maintain/write). Bots excluded from all human signals.")
    print("silent = days since the AUTHOR last moved · idle = days since ANYONE moved · "
          "others = maintainers engaged besides you and the author")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", action="append", default=[])
    ap.add_argument("--unacted", action="store_true")
    ap.add_argument("--stalled", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--me", default=None)
    a = ap.parse_args()

    me = a.me or sh(["gh", "api", "user", "--jq", ".login"]).strip()
    maintainers = load_maintainers()
    cards = load_dashboard()
    prs = a.pr or sorted(cards, key=int)
    if not prs:
        print("No PRs: pass --pr N, or start the dashboard so reviewed PRs are listed.",
              file=sys.stderr)
        return 1

    rows = []
    for pr in prs:
        try:
            rows.append(classify(str(pr), me, maintainers, cards.get(str(pr), {})))
        except Exception as e:  # noqa: BLE001
            print(f"  (#{pr}: {e})", file=sys.stderr)

    if a.unacted:
        rows = [r for r in rows if not r["our_acted"] and not r["draft"]]
    if a.stalled:
        rows = [r for r in rows if (r["author_silent_days"] or 0) >= 14]

    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        render(rows, me)
    return 0


if __name__ == "__main__":
    sys.exit(main())
