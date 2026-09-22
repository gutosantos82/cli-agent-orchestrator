#!/usr/bin/env python3
"""Repair a review report that is missing its YAML frontmatter block.

Why this exists: the supervisor is instructed to write the triage fields as YAML
frontmatter, and it intermittently writes a markdown report instead, stating the
verdict as prose under a `## Verdict` / `## Recommendation` heading. When that
happens the review is complete and good, but `pr_report.py` and
`publish_reviews.sh` parse only the frontmatter block, so the review is INVISIBLE
to them and is silently never published.

Prompt instructions did not hold this: the same fix produced 3/3 well-formed
reports one night and 0/4 the next. So repair rather than re-ask — the report body
already contains everything the frontmatter needs.

Deliberately conservative. This only ever ADDS a frontmatter block to a file that
has none; it never edits an existing block, never rewrites the body, and never
invents a verdict it cannot find. If the verdict is unreadable it leaves the file
alone and says so, because a wrong verdict posted under a maintainer's name is far
worse than an unpublished review.

Usage:  repair_frontmatter.py REPORT.md [REPORT.md ...]
Exit:   0 if every file is now well-formed (repaired or already fine)
        1 if any file still needs a human
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The only three values the tooling understands (publish_reviews.sh maps these to
# a gh review action). Order matters: longest/most-specific first so "Approve with
# nits" is never shortened to "Approve".
LEGAL = ("Approve with nits", "Request changes", "Approve")

# Prose the supervisor has actually produced, mapped to the legal value it means.
# Checked in order, case-insensitively, against the verdict region.
#
# The negatives MUST come before "approve": "DO NOT APPROVE" and "NOT READY TO
# APPROVE" both contain the substring "approve", so a naive approve-check first
# would invert the verdict — the single worst failure mode available here.
PROSE = (
    (r"do not approve", "Request changes"),
    (r"not ready to approve", "Request changes"),
    (r"withhold approval", "Request changes"),
    (r"request[- ]changes", "Request changes"),
    (r"\brequest changes\b", "Request changes"),
    (r"\bdefer\b", "Request changes"),
    (r"approve with nits?", "Approve with nits"),
    (r"approve with minor", "Approve with nits"),
    (r"\bapprove\b", "Approve"),
)

HEADING = re.compile(r"^#+\s*(verdict|recommendation)\b", re.IGNORECASE)


def find_verdict(text: str) -> tuple[str | None, str]:
    """Return (legal_verdict, evidence). legal_verdict is None if undecidable."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not HEADING.match(line):
            continue
        # The verdict may sit on the heading itself ("## Verdict: DO NOT APPROVE")
        # or in the prose beneath a bare "## Verdict". Scan the heading plus the
        # next few non-blank lines, stopping at the next heading so we never read
        # a later section's language.
        region = [line]
        for nxt in lines[i + 1 : i + 8]:
            if nxt.startswith("#"):
                break
            if nxt.strip():
                region.append(nxt)
        blob = " ".join(region)
        for pattern, verdict in PROSE:
            if re.search(pattern, blob, re.IGNORECASE):
                snippet = re.sub(r"\s+", " ", blob).strip()[:100]
                return verdict, snippet
    return None, ""


def title_of(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            # "# PR #802 Review — Feat/745 ..." -> keep the descriptive tail
            t = line.lstrip("# ").strip()
            t = re.sub(r"^PR (Review: )?#?\d+ *(Review)? *[—–-] *", "", t).strip()
            return t.replace('"', "'") or "untitled"
    return "untitled"


def repair(path: Path) -> bool:
    text = path.read_text()
    if text.startswith("---"):
        return True  # already well-formed; not ours to touch

    verdict, evidence = find_verdict(text)
    if verdict is None:
        print(f"  ✗ {path.name}: no readable verdict — left unchanged, needs a human")
        return False

    # needs_human is set true on every repaired report. The frontmatter was
    # reconstructed rather than authored, so the approve guard should hold it for
    # a human read even when the verdict itself is unambiguous.
    block = (
        "---\n"
        f'title: "{title_of(text)}"\n'
        "urgency: medium\n"
        "importance: medium\n"
        f'verdict: "{verdict}"\n'
        "needs_human: true\n"
        'needs_human_reason: "Frontmatter was reconstructed by repair_frontmatter.py '
        "from the report's prose verdict — the supervisor omitted the YAML block. "
        'Verify the verdict against the report body before approving."\n'
        f'summary: "{evidence.replace(chr(34), chr(39))}"\n'
        "---\n\n"
    )
    path.write_text(block + text)
    print(f"  ✓ {path.name}: frontmatter added (verdict: {verdict})")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    ok = True
    for arg in argv:
        p = Path(arg)
        if not p.is_file():
            print(f"  ✗ {arg}: not a file")
            ok = False
            continue
        ok = repair(p) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
