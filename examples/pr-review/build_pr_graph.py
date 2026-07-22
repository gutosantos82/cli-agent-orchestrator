#!/usr/bin/env python3
"""Build a PR relationship graph for the dashboard /graph view.

Nodes  = PRs (open + recent merged/closed) and the issues they reference.
Edges  = typed relationships:
  - reference   : PR mentions another PR in its title/body (kind: fixes/closes/refs/
                  followup/supersedes/partof/mentions) — the "is this a follow-up?" signal.
  - issue       : PR addresses an issue (#N that is not itself a PR in the window).
  - file-overlap: two PRs touch overlapping files, idf-weighted so ubiquitous files
                  (README, pyproject) count little and co-touched RARE files count a lot —
                  the "does this build on the same code as prior PRs?" signal.

Each OPEN PR is classified follow-up / builds-on / related / new so the operator can
judge roadmap fit at a glance.

Data comes from `gh` (one list call + parallel per-PR file calls) and our own review
reports (for the verdict on open PRs). Writes <data-dir>/graph.json. Stdlib only.
"""
import argparse
import json
import math
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# "<keyword> #123" — captures the relationship kind + target number.
KW_RE = re.compile(
    r"\b(fix(?:e[sd])?|close[sd]?|resolve[sd]?|ref(?:s|erences)?|part of|follow[- ]?up(?: to)?|"
    r"supersede[sd]?|replace[sd]?|based on|built on|continu(?:es|ation of)|depends on)\b[:\s]*#(\d+)",
    re.IGNORECASE,
)
ANY_RE = re.compile(r"#(\d+)")

KIND_MAP = {
    "fix": "fixes", "fixe": "fixes", "fixes": "fixes", "fixed": "fixes",
    "close": "closes", "closes": "closes", "closed": "closes",
    "resolve": "closes", "resolves": "closes", "resolved": "closes",
    "ref": "refs", "refs": "refs", "references": "refs", "reference": "refs",
    "part of": "partof",
    "followup": "followup", "follow up": "followup", "follow-up": "followup",
    "followup to": "followup", "follow up to": "followup", "follow-up to": "followup",
    "supersede": "supersedes", "supersedes": "supersedes", "superseded": "supersedes",
    "replace": "supersedes", "replaces": "supersedes", "replaced": "supersedes",
    "based on": "buildson", "built on": "buildson", "continues": "buildson",
    "continuation of": "buildson", "depends on": "buildson",
}
# kinds that indicate the PR extends prior work (drives "follow-up" classification)
FOLLOWUP_KINDS = {"followup", "supersedes", "partof", "buildson"}


def gh_json(args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "null")


def fetch_files(repo, num):
    try:
        data = gh_json(["pr", "view", str(num), "--repo", repo, "--json", "files"])
        return num, [f["path"] for f in (data or {}).get("files", [])]
    except Exception:
        return num, []


def norm_kind(word):
    return KIND_MAP.get(word.lower().strip(), "refs")


def parse_refs(text):
    """Return {target_num: kind}. Keyworded refs win over bare mentions."""
    refs = {}
    for m in ANY_RE.finditer(text or ""):
        refs.setdefault(int(m.group(1)), "mentions")
    for m in KW_RE.finditer(text or ""):
        refs[int(m.group(2))] = norm_kind(m.group(1))
    return refs


def load_report_verdicts(data_dir, open_nums):
    """Map open PR number -> our latest review verdict (best-effort, from report frontmatter)."""
    verdicts = {}
    rdir = data_dir / "reviews"
    if not rdir.exists():
        return verdicts
    # newest report file per PR by mtime
    latest = {}
    for f in rdir.glob("*.md"):
        pr, _, _sha = f.stem.partition("-")
        if not pr.isdigit():
            continue
        n = int(pr)
        if n not in open_nums:
            continue
        mt = f.stat().st_mtime
        if n not in latest or latest[n][0] < mt:
            latest[n] = (mt, f)
    for n, (_mt, f) in latest.items():
        try:
            txt = f.read_text()
            m = re.search(r'^verdict:\s*"?([^"\n]+)"?\s*$', txt.split("---", 2)[1], re.MULTILINE)
            if m:
                verdicts[n] = m.group(1).strip()
        except Exception:
            pass
    return verdicts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="awslabs/cli-agent-orchestrator")
    ap.add_argument("--limit", type=int, default=150, help="window of PRs (all states, newest first)")
    ap.add_argument("--file-window", type=int, default=120, help="fetch touched files for the newest N PRs")
    ap.add_argument("--data-dir", default="pr-review-data")
    ap.add_argument("--min-overlap", type=float, default=1.0,
                    help="minimum idf-weighted overlap score for a file-overlap edge")
    ap.add_argument("--max-overlap-per-node", type=int, default=6,
                    help="keep only the K strongest file-overlap edges per PR (limits hairball)")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching newest {args.limit} PRs (all states) from {args.repo}…", file=sys.stderr)
    prs = gh_json(["pr", "list", "--repo", args.repo, "--state", "all", "--limit", str(args.limit),
                   "--json", "number,title,state,author,createdAt,closedAt,mergedAt,body,labels,isDraft"])
    prs = prs or []
    pr_nums = {p["number"] for p in prs}
    open_nums = {p["number"] for p in prs if p["state"] == "OPEN"}
    # Highest number assigned in the repo (issues + PRs share one sequence), used to reject
    # bogus "#12345" refs parsed from numbers/code in PR bodies. Small margin for very recent
    # issues not in the PR window.
    max_num = (max(pr_nums) if pr_nums else 0) + 25

    # touched files for the newest file-window PRs (parallel)
    window = [p["number"] for p in prs][: args.file_window]
    print(f"Fetching touched files for {len(window)} PRs…", file=sys.stderr)
    files = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for num, paths in ex.map(lambda n: fetch_files(args.repo, n), window):
            files[num] = paths

    # document frequency per file (how many PRs in the window touch it) → idf weight
    df = {}
    for paths in files.values():
        for p in set(paths):
            df[p] = df.get(p, 0) + 1
    n_docs = max(len(files), 1)
    idf = {f: math.log(1 + n_docs / c) for f, c in df.items()}

    verdicts = load_report_verdicts(data_dir, open_nums)

    # ---- nodes ----
    nodes = {}
    for p in prs:
        num = p["number"]
        text = f"{p.get('title','')} {p.get('body','') or ''}"
        refs = parse_refs(text)
        refs.pop(num, None)  # no self-reference
        refs = {n: k for n, k in refs.items() if 0 < n <= max_num}  # drop bogus large #s
        pr_refs = {n: k for n, k in refs.items() if n in pr_nums}
        issue_refs = {n: k for n, k in refs.items() if n not in pr_nums}
        nodes[num] = {
            "id": num, "type": "pr",
            "title": p.get("title", ""),
            "state": p.get("state", ""),
            "draft": p.get("isDraft", False),
            "author": (p.get("author") or {}).get("login", ""),
            "createdAt": p.get("createdAt", ""),
            "mergedAt": p.get("mergedAt", ""),
            "closedAt": p.get("closedAt", ""),
            "labels": [l["name"] for l in (p.get("labels") or [])],
            "open": p.get("state") == "OPEN",
            "verdict": verdicts.get(num, ""),
            "files": files.get(num, []),
            "n_files": len(files.get(num, [])),
            "pr_refs": pr_refs,
            "issue_refs": issue_refs,
        }

    # ---- edges ----
    edges = []
    # reference edges (PR -> PR)
    for num, nd in nodes.items():
        for tgt, kind in nd["pr_refs"].items():
            edges.append({"source": num, "target": tgt, "type": "reference", "kind": kind})

    # issue nodes + edges (PR -> issue), for issues referenced with a real keyword OR shared by >=2 PRs
    issue_reffers = {}
    for num, nd in nodes.items():
        for iss, kind in nd["issue_refs"].items():
            issue_reffers.setdefault(iss, []).append((num, kind))
    issue_nodes = {}
    for iss, reffers in issue_reffers.items():
        keyworded = any(k != "mentions" for _n, k in reffers)
        if keyworded or len(reffers) >= 2:
            iid = f"i{iss}"
            issue_nodes[iid] = {"id": iid, "type": "issue", "number": iss,
                                "title": f"issue #{iss}", "open": False}
            for n, kind in reffers:
                edges.append({"source": n, "target": iid, "type": "issue", "kind": kind})

    # file-overlap edges (idf-weighted, top-K per node)
    file_nums = [n for n in window if files.get(n)]
    cand = {}  # (a,b) -> score
    # invert: file -> PRs, then only compare PRs that share at least one file
    file_to_prs = {}
    for n in file_nums:
        for f in set(files[n]):
            file_to_prs.setdefault(f, []).append(n)
    for f, plist in file_to_prs.items():
        if len(plist) < 2:
            continue
        w = idf.get(f, 0.0)
        for i in range(len(plist)):
            for j in range(i + 1, len(plist)):
                a, b = sorted((plist[i], plist[j]))
                cand[(a, b)] = cand.get((a, b), 0.0) + w
    # keep top-K per node above threshold
    per_node = {}
    for (a, b), score in cand.items():
        if score < args.min_overlap:
            continue
        per_node.setdefault(a, []).append((score, b))
        per_node.setdefault(b, []).append((score, a))
    kept = set()
    for n, lst in per_node.items():
        lst.sort(reverse=True)
        for score, other in lst[: args.max_overlap_per_node]:
            kept.add((min(n, other), max(n, other)))
    for (a, b) in kept:
        edges.append({"source": a, "target": b, "type": "file-overlap",
                      "weight": round(cand[(a, b)], 2)})

    # ---- classify open PRs (follow-up / builds-on / related / new) ----
    merged = {n for n, nd in nodes.items() if nd["state"] == "MERGED"}
    overlap_adj = {}
    for (a, b) in kept:
        overlap_adj.setdefault(a, set()).add(b)
        overlap_adj.setdefault(b, set()).add(a)
    for num, nd in nodes.items():
        if not nd["open"]:
            nd["classification"] = ""
            continue
        kinds = set(nd["pr_refs"].values())
        refs_merged = any(t in merged for t in nd["pr_refs"])
        shares_issue_with_others = any(
            len(issue_reffers.get(iss, [])) >= 2 for iss in nd["issue_refs"]
        )
        overlaps_merged = any(o in merged for o in overlap_adj.get(num, ()))
        if kinds & FOLLOWUP_KINDS or (refs_merged and nd["pr_refs"]):
            nd["classification"] = "follow-up"
        elif nd["pr_refs"] or shares_issue_with_others:
            nd["classification"] = "related"
        elif overlaps_merged:
            nd["classification"] = "builds-on"
        else:
            nd["classification"] = "new"

    all_nodes = list(nodes.values()) + list(issue_nodes.values())
    out = {
        "generated_at": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                                        capture_output=True, text=True).stdout.strip(),
        "repo": args.repo,
        "counts": {
            "prs": len(nodes), "issues": len(issue_nodes), "edges": len(edges),
            "open": len(open_nums), "with_files": len(file_nums),
        },
        "nodes": all_nodes,
        "edges": edges,
    }
    out_path = data_dir / "graph.json"
    out_path.write_text(json.dumps(out, indent=1))
    print(f"Wrote {out_path}: {len(all_nodes)} nodes ({len(nodes)} PRs + {len(issue_nodes)} issues), "
          f"{len(edges)} edges.", file=sys.stderr)


if __name__ == "__main__":
    main()
