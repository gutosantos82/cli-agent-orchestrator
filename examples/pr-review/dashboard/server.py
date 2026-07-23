#!/usr/bin/env python3
"""PR Review Dashboard — triage + act on CAO PR reviews.

The pr_review_manager writes per-PR metadata to `<data-dir>/meta/<pr>-<sha>.json`
(deterministic facts: size, days waiting, author reputation, CI, labels). The
pr_review_supervisor writes a review report to `<data-dir>/reviews/<pr>-<sha>.md`
with a YAML frontmatter block carrying the LLM-judged fields (urgency, importance,
one-line summary). This server merges both, shows a cards grid for triage, and lets
you drill into the full review and act via gh.

Run:
    uv run --no-project --with fastapi --with uvicorn --with markdown --with pyyaml \
        examples/pr-review/dashboard/server.py

Safety:
    Dry-run by default — Approve/Comment/Request only PRINT the gh command.
    Pass --execute to actually post to GitHub.
"""
import argparse
import html
import json
import re
import subprocess
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import markdown as md
import uvicorn

STATE = {"repo": "", "data_dir": Path("."), "execute": False}

# Route dashboard actions through the guarded publisher (stale-head re-check,
# human-notes stripping, state.json sync) instead of raw gh.
REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLISH_SCRIPT = str(Path(__file__).resolve().parents[1] / "publish_reviews.sh")

# Level-2 sections that publish_reviews.sh strips from the posted comment — kept in
# the report for the dashboard only. Must mirror strip_human_notes in publish_reviews.sh.
DASHBOARD_ONLY_RE = re.compile(
    r"notes? for the human|human publisher|publisher note|do not post|internal[ -]only|"
    r"reviewer note|prior feedback|already raised|publish[ -]?guard|roadmap|vision fit",
    re.I,
)


def render_body_marked(body: str) -> str:
    """Render the report markdown, wrapping each dashboard-only section (the ones
    NOT posted to GitHub) in a visually-distinct block so the human can see at a
    glance what reaches the PR vs what stays on the dashboard."""
    if not body:
        return "<p><em>Deep review pending — metadata only so far.</em></p>"
    out = []
    for seg in re.split(r"(?m)^(?=##\s)", body):
        if not seg.strip():
            continue
        first = seg.lstrip().splitlines()[0]
        rendered = md.markdown(seg, extensions=["fenced_code", "tables"])
        if first.startswith("##") and DASHBOARD_ONLY_RE.search(first):
            out.append(
                '<div class="dash-only"><div class="dash-only-tag">'
                '🔒 dashboard-only · not posted to GitHub</div>'
                f'{rendered}</div>'
            )
        else:
            out.append(rendered)
    return "".join(out)

URGENCY_RANK = {"high": 0, "medium": 1, "low": 2, "": 3}
URGENCY_COLOR = {"high": "#cf222e", "medium": "#9a6700", "low": "#1a7f37"}
IMPORTANCE_COLOR = {"high": "#8250df", "medium": "#0969da", "low": "#656d76"}


def data() -> Path:
    return STATE["data_dir"]


def load_state() -> dict:
    f = data() / "state.json"
    return json.loads(f.read_text()) if f.exists() else {}


def load_security() -> dict:
    """Load the repo-level code-scanning summary written by run_reviews.sh (security.json)."""
    f = data() / "security.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading `---\\n…\\n---` YAML block from the markdown body."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n")
            except yaml.YAMLError:
                pass
    return {}, text


_OPEN_CACHE = {"prs": None, "at": 0.0}


def open_pr_numbers() -> set[str] | None:
    """Live set of open PR numbers (cached 60s). None if gh is unavailable — in which case
    we don't filter (fail open). Used to drop cards for PRs that have merged/closed."""
    import time
    now = time.time()
    if _OPEN_CACHE["prs"] is not None and now - _OPEN_CACHE["at"] < 60:
        return _OPEN_CACHE["prs"]
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--repo", STATE["repo"], "--state", "open",
             "--limit", "200", "--json", "number", "--jq", ".[].number"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return _OPEN_CACHE["prs"]  # keep last good (or None)
        prs = {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}
        _OPEN_CACHE.update(prs=prs, at=now)
        return prs
    except Exception:  # noqa: BLE001
        return _OPEN_CACHE["prs"]


def list_prs() -> list[dict]:
    """One merged entry per PR: review report + frontmatter + manager metadata.
    Only PRs currently open on the repo are shown (closed/merged cards are pruned)."""
    state = load_state()
    open_prs = open_pr_numbers()  # None => gh unavailable, show everything (fail open)
    # collect metadata (manager) and reviews (supervisor) by PR; either may be absent.
    # Collect metadata (manager) and reviews (supervisor) per PR, keeping the NEWEST
    # file by mtime. Files are named "<pr>-<sha>.<ext>" and a PR accrues one file per
    # SHA across successive runs, so lexicographic filename order is meaningless — the
    # freshest review is the most recently written file, not the one with the largest
    # SHA hex. (Bug: `sorted(glob())[last]` pinned cards to a stale review whenever an
    # older SHA sorted after the current head, e.g. c3e5222 after 19fe981.)
    metas, reviews = {}, {}
    meta_mtime, review_mtime = {}, {}
    meta_dir = data() / "meta"
    if meta_dir.exists():
        for mf in meta_dir.glob("*.json"):
            pr, _, sha = mf.stem.partition("-")
            if not pr.isdigit():
                continue
            mtime = mf.stat().st_mtime
            if pr in meta_mtime and meta_mtime[pr] >= mtime:
                continue
            try:
                parsed = json.loads(mf.read_text())
            except json.JSONDecodeError:
                continue
            metas[pr] = (sha, parsed)
            meta_mtime[pr] = mtime
    for f in (data() / "reviews").glob("*.md"):
        pr, _, sha = f.stem.partition("-")
        if not pr.isdigit():
            continue
        mtime = f.stat().st_mtime
        if pr in review_mtime and review_mtime[pr] >= mtime:
            continue
        reviews[pr] = (sha, *parse_frontmatter(f.read_text()))  # (sha, frontmatter, body)
        review_mtime[pr] = mtime

    out = []
    for pr in set(metas) | set(reviews):
        if open_prs is not None and pr not in open_prs:
            continue  # PR merged/closed — prune its stale card
        meta_sha, meta = metas.get(pr, ("", {}))
        rev = reviews.get(pr)
        if rev:
            rev_sha, fm, body = rev
        else:
            rev_sha, fm, body = "", {}, ""
        sha = rev_sha or meta_sha          # prefer the reviewed SHA when present
        has_review = bool(rev)
        st = state.get(pr, {})
        out.append({
            "pr": pr,
            "sha": sha,
            "has_review": has_review,
            # judged fields (from review frontmatter; blank until the review lands)
            "title": fm.get("title") or meta.get("title") or f"PR #{pr}",
            "urgency": str(fm.get("urgency", "")).lower(),
            "importance": str(fm.get("importance", "")).lower(),
            "verdict": fm.get("verdict", ""),
            "fit": str(fm.get("fit", "")).lower().strip(),
            "summary": fm.get("summary", "") or ("Review pending…" if not has_review else ""),
            # deterministic fields (from manager metadata)
            "size": meta.get("size", ""),
            "additions": meta.get("additions"),
            "deletions": meta.get("deletions"),
            "files": meta.get("files"),
            "days_waiting": meta.get("days_waiting"),
            "author": meta.get("author", ""),
            "author_merged_prs": meta.get("author_merged_prs"),
            "ci": meta.get("ci", ""),
            "labels": meta.get("labels", []),
            "draft": meta.get("draft", False),
            "comments": meta.get("comments"),
            "reviews_count": meta.get("reviews"),
            "code_changed": meta.get("code_changed", False),
            "human_activity": meta.get("human_activity", False),
            # human-engagement + mergeability signals
            "mergeable": meta.get("mergeable", ""),
            "review_decision": meta.get("review_decision", ""),
            "human_reviewers": meta.get("human_reviewers", []) or [],
            "last_human": meta.get("last_human"),
            # review body + action state
            "html": render_body_marked(body),
            "raw": body,
            "acted": st.get("acted"),
            "acted_sha": st.get("acted_sha"),
            "acted_at": st.get("acted_at"),
            "stale": bool(st.get("acted_sha") and sha and st.get("acted_sha") != sha),
        })
    # sort: reviewed-first, then urgency (high→low), then longest-waiting
    return sorted(
        out,
        key=lambda r: (0 if r["has_review"] else 1,
                       URGENCY_RANK.get(r["urgency"], 3),
                       -(r["days_waiting"] or 0)),
    )


def run_gh(args: list[str]) -> dict:
    cmd = ["gh", *args]
    if not STATE["execute"]:
        return {"ok": True, "output": f"[DRY-RUN] would run: {' '.join(cmd)}"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        ok = r.returncode == 0
        return {"ok": ok, "output": (r.stdout + r.stderr).strip() or ("done" if ok else "failed")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": str(e)}


def record_action(pr: str, sha: str, action: str) -> None:
    import datetime
    f = data() / "state.json"
    state = load_state()
    entry = state.get(pr, {})
    entry.update({
        "acted": action,
        "acted_sha": sha,
        "acted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    })
    state[pr] = entry
    f.write_text(json.dumps(state, indent=2))


app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render_page(list_prs())


@app.get("/api/prs")
def api_prs() -> JSONResponse:
    return JSONResponse(list_prs())


@app.get("/api/graph")
def api_graph() -> JSONResponse:
    """Serve the prebuilt PR relationship graph (from build_pr_graph.sh)."""
    f = data() / "graph.json"
    if not f.exists():
        return JSONResponse({"nodes": [], "edges": [], "counts": {},
                             "error": "graph.json not found — run examples/pr-review/build_pr_graph.sh"})
    try:
        return JSONResponse(json.loads(f.read_text()))
    except json.JSONDecodeError as e:
        return JSONResponse({"nodes": [], "edges": [], "counts": {}, "error": str(e)})


@app.get("/graph", response_class=HTMLResponse)
def graph_page() -> str:
    return render_graph_page()


@app.post("/api/action")
async def api_action(req: Request) -> JSONResponse:
    d = await req.json()
    pr, action = str(d["pr"]), d["action"]
    repo = STATE["repo"]
    if action not in ("approve", "request", "comment"):
        return JSONResponse({"ok": False, "output": f"unknown action {action}"}, status_code=400)
    # Guarded path: publish_reviews.sh re-checks the current head, strips
    # operator-only notes, posts the report body, and syncs state.json. The
    # dashboard click == consent (approve bypasses the needs_human/size hold, as
    # with the Telegram button), but stale-head safety is preserved.
    cmd = ["bash", PUBLISH_SCRIPT, "--repo", repo, "--action", action, pr]
    if not STATE["execute"]:
        return JSONResponse({"ok": True, "output": f"[DRY-RUN] would run: {' '.join(cmd)}"})
    try:
        r = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        blob = (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "output": str(e)})
    ok = "POSTED (" in blob
    # publish_reviews.sh already synced state.json on success; surface the tail.
    return JSONResponse({"ok": ok, "output": blob[-800:] or ("done" if ok else "no output")})


def pill(text: str, color: str) -> str:
    return f'<span class="pill" style="background:{color}">{html.escape(str(text))}</span>'


def render_page(prs: list[dict]) -> str:
    mode = "EXECUTE — actions hit GitHub" if STATE["execute"] else "DRY-RUN — actions are simulated"
    mode_cls = "exec" if STATE["execute"] else "dry"
    # Repo-level code-scanning summary banner (from security.json).
    sec = load_security()
    sec_banner = ""
    if sec:
        if not sec.get("available", False):
            sec_banner = ('<div class="secbar unavail">🔒 Code scanning: unavailable '
                          f'(needs security_events scope) · <a href="{html.escape(sec.get("url",""))}" '
                          'target="_blank" rel="noopener">Security tab ↗</a></div>')
        else:
            total = sec.get("total_open", 0)
            sev_order = ["critical", "high", "medium", "low", "warning", "note"]
            sev_color = {"critical": "#cf222e", "high": "#cf222e", "medium": "#9a6700",
                         "low": "#1a7f37", "warning": "#9a6700", "note": "#57606a"}
            by_sev = sec.get("by_severity", {}) or {}
            sev_pills = "".join(
                f'<span class="pill" style="background:{sev_color.get(s, "#57606a")}">{s} {by_sev[s]}</span>'
                for s in sev_order if by_sev.get(s)
            )
            # any severity keys not in the known order
            sev_pills += "".join(
                f'<span class="pill" style="background:#57606a">{html.escape(str(s))} {n}</span>'
                for s, n in by_sev.items() if s not in sev_order and n
            )
            tops = "".join(
                f'<li><a href="{html.escape(a.get("url",""))}" target="_blank" rel="noopener">'
                f'#{a.get("number")}</a> <b>{html.escape(str(a.get("severity","")))}</b> '
                f'{html.escape(str(a.get("rule","")))} '
                f'<span class="secpath">{html.escape(str(a.get("path","")))}:{a.get("line","")}</span></li>'
                for a in sec.get("top_alerts", [])
            )
            cls = "ok" if total == 0 else "alert"
            headline = ("✅ Code scanning: 0 open alerts" if total == 0
                        else f"🔒 Code scanning: {total} open alert(s)")
            details = (f'<details><summary>top {min(total, 8)}</summary><ul class="seclist">{tops}</ul></details>'
                       if total else "")
            sec_banner = (
                f'<div class="secbar {cls}">'
                f'<span class="sechead">{headline}</span> {sev_pills} '
                f'<a href="{html.escape(sec.get("url",""))}" target="_blank" rel="noopener">Security tab ↗</a>'
                f'{details}</div>'
            )
    cards = []
    for r in prs:
        flags = []
        if r["urgency"]:
            flags.append(pill(f"⚑ {r['urgency']}", URGENCY_COLOR.get(r["urgency"], "#656d76")))
        if r["importance"]:
            flags.append(pill(f"★ {r['importance']}", IMPORTANCE_COLOR.get(r["importance"], "#656d76")))
        if r["size"]:
            extra = f" {r['additions']}+/{r['deletions']}-" if r["additions"] is not None else ""
            flags.append(pill(f"{r['size']}{extra}", "#57606a"))
        if r["days_waiting"] is not None:
            flags.append(pill(f"⏱ {r['days_waiting']}d", "#57606a"))
        if r["author"]:
            rep = f" ({r['author_merged_prs']} merged)" if r["author_merged_prs"] is not None else ""
            flags.append(pill(f"@{r['author']}{rep}", "#57606a"))
        if r["ci"]:
            ci_color = {"passing": "#1a7f37", "failing": "#cf222e"}.get(r["ci"], "#57606a")
            flags.append(pill(f"CI {r['ci']}", ci_color))
        if r["comments"]:
            flags.append(pill(f"💬 {r['comments']}", "#57606a"))
        if r["code_changed"]:
            flags.append(pill("🔁 code changed — re-review", "#cf222e"))
        if r["human_activity"]:
            flags.append(pill("💬 discussion since review", "#8250df"))
        if r["draft"]:
            flags.append(pill("draft", "#9a6700"))
        for lb in r["labels"]:
            flags.append(pill(lb, "#6e7781"))
        # review-status badge: is the LLM review done, or only metadata so far?
        if r["has_review"]:
            review_badge = '<span class="badge reviewed">✓ reviewed</span>'
        else:
            review_badge = '<span class="badge pending">⏳ review pending</span>'
        # action badge: what the human did + WHEN (approved/commented/requested @ date)
        acted_badge = ""
        if r["acted"]:
            cls = "stale" if r["stale"] else "done"
            extra = " · stale (PR changed)" if r["stale"] else ""
            when_short, when_full = "", ""
            if r["acted_at"]:
                when_full = r["acted_at"]                       # full ISO for the tooltip
                when_short = f" · {r['acted_at'][5:10]}"        # MM-DD in the badge
            acted_badge = (f'<span class="badge {cls}" title="{html.escape(when_full)}">'
                           f'{r["acted"]}{when_short}{extra}</span>')
        # Verdict badge — rendered from frontmatter so it's always visible, even
        # when the report body has no "## Verdict" section (e.g. #428).
        verdict_badge = ""
        if r["verdict"]:
            _vl = r["verdict"].lower()
            _vc = ("#cf222e" if "request" in _vl else "#9a6700" if "nits" in _vl
                   else "#1a7f37" if "approve" in _vl else "#6e7781")
            verdict_badge = (f'<span class="badge" style="background:{_vc};color:#fff">'
                             f'{html.escape(r["verdict"])}</span>')
        # vision/mission fit badge (advisory, from vision_reviewer)
        fit_badge = ""
        if r["fit"]:
            _fc = {"core": "#1a7f37", "adjacent": "#0969da",
                   "scope-creep": "#9a6700", "off-mission": "#cf222e"}.get(r["fit"], "#6e7781")
            fit_badge = (f'<span class="badge" style="background:{_fc};color:#fff" '
                         f'title="Strategic fit vs CAO vision (advisory)">🎯 {html.escape(r["fit"])}</span>')
        # --- Human-engagement + mergeability badges (deterministic GitHub signals) ---
        signal_badges = ""
        conflicting = str(r["mergeable"]).upper() == "CONFLICTING"
        if conflicting:
            signal_badges += '<span class="badge" style="background:#cf222e;color:#fff" title="Head conflicts with the base branch">⚠️ conflicts</span>'
        rd = (r["review_decision"] or "").upper()
        who = ", ".join(sorted({hr.get("login", "") for hr in (r["human_reviewers"] or []) if hr.get("login")}))
        who_e = html.escape(who)
        if rd == "APPROVED":
            signal_badges += f'<span class="badge" style="background:#1a7f37;color:#fff" title="A human approved this PR on GitHub">👤 human-approved{(" · " + who_e) if who else ""}</span>'
        elif rd == "CHANGES_REQUESTED":
            signal_badges += f'<span class="badge" style="background:#cf222e;color:#fff" title="A human requested changes on GitHub">👤 human: changes requested{(" · " + who_e) if who else ""}</span>'
        elif who:
            signal_badges += f'<span class="badge" style="background:#0969da;color:#fff" title="A human is already reviewing this PR">👤 reviewing: {who_e}</span>'
        # verdict-vs-human mismatch: our verdict disagrees with the human decision.
        _vlow = (r["verdict"] or "").lower()
        mismatch = ((rd == "APPROVED" and "request" in _vlow)
                    or (rd == "CHANGES_REQUESTED" and "approve" in _vlow))
        if mismatch:
            signal_badges += '<span class="badge" style="background:#bf3989;color:#fff" title="Our verdict disagrees with the human review decision on GitHub">⚠️ verdict vs human</span>'
        has_human = bool(who) or rd in ("APPROVED", "CHANGES_REQUESTED")
        # latest human comment/review note
        last_human_line = ""
        lh = r["last_human"]
        if isinstance(lh, dict) and lh.get("note"):
            _when = (lh.get("at") or "")[:10]
            last_human_line = (f'<p class="lasthuman">💬 <b>{html.escape(lh.get("login",""))}</b>'
                               f'{(" · " + _when) if _when else ""}: {html.escape(lh["note"])}</p>')
        cards.append(f"""
        <article class="card" data-pr="{r['pr']}" data-sha="{r['sha']}"
                 data-raw="{html.escape(json.dumps(r['raw']))}"
                 data-html="{html.escape(json.dumps(r['html']))}"
                 data-title="{html.escape(r['title'])}" data-verdict="{html.escape(r['verdict'])}"
                 data-acted="{html.escape(json.dumps(r['acted'] or ''))}"
                 data-acted-at="{html.escape(json.dumps(r['acted_at'] or ''))}"
                 data-f-urgency="{r['urgency'] or 'none'}"
                 data-f-ci="{r['ci'] or 'none'}"
                 data-f-verdict="{('request' if 'request' in (r['verdict'] or '').lower() else 'approve' if 'approve' in (r['verdict'] or '').lower() else 'none')}"
                 data-f-status="{'reviewed' if r['has_review'] else 'pending'}"
                 data-f-acted="{'acted' if (r['acted'] and not r['stale']) else 'unacted'}"
                 data-f-pending="{'yes' if (r['has_review'] and not r['code_changed'] and not (r['acted'] and not r['stale'])) else 'no'}"
                 data-f-attention="{'code' if r['code_changed'] else 'discussion' if r['human_activity'] else 'none'}"
                 data-f-conflicts="{'yes' if conflicting else 'no'}"
                 data-f-human="{'yes' if has_human else 'no'}"
                 data-f-mismatch="{'yes' if mismatch else 'no'}"
                 data-f-fit="{r['fit'] or 'none'}"
                 data-f-text="{html.escape((str(r['pr']) + ' ' + (r['title'] or '') + ' ' + (r['author'] or '')).lower())}"
                 onclick="openDetail(this)">
          <div class="card-top"><span class="num">#{r['pr']}</span><span class="badges">{review_badge}{verdict_badge}{fit_badge}{signal_badges}{acted_badge}</span></div>
          <h3>{html.escape(r['title'])}</h3>
          <p class="summary">{html.escape(r['summary'] or r['verdict'] or '')}</p>
          {last_human_line}
          <div class="flags">{''.join(flags)}</div>
        </article>""")
    grid = "\n".join(cards) or "<p class='empty'>No reviews yet. Run the pr_review_manager.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>CAO PR Triage — {STATE['repo']}</title>
<style>
  body {{ font:15px/1.5 -apple-system,system-ui,sans-serif; margin:0; background:#f6f8fa; color:#1f2328; }}
  .topbar {{ position:sticky; top:0; z-index:5; background:#24292f; color:#fff; padding:12px 20px; display:flex; justify-content:space-between; align-items:center; }}
  .topbar h1 {{ font-size:16px; margin:0; }}
  .mode {{ font-size:12px; padding:3px 10px; border-radius:12px; }}
  .mode.dry {{ background:#9a6700; }} .mode.exec {{ background:#cf222e; }}
  main {{ max-width:1100px; margin:20px auto; padding:0 16px; }}
  .filterbar {{ position:sticky; top:45px; z-index:4; background:#eaeef2; border-bottom:1px solid #d0d7de;
    padding:8px 20px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }}
  .filterbar select, .filterbar input {{ font:13px inherit; padding:4px 8px; border:1px solid #d0d7de; border-radius:6px; background:#fff; }}
  .filterbar input.search {{ min-width:200px; }}
  .filterbar label {{ font-size:12px; color:#57606a; }}
  #f-count {{ margin-left:auto; font-size:12px; color:#57606a; }}
  #f-reset {{ cursor:pointer; border:1px solid #d0d7de; border-radius:6px; padding:4px 10px; background:#fff; font:13px inherit; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); gap:14px; }}
  .card.hidden {{ display:none; }}
  .card {{ background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:14px; cursor:pointer; transition:box-shadow .15s,border-color .15s; }}
  .card:hover {{ box-shadow:0 3px 12px rgba(0,0,0,.1); border-color:#0969da; }}
  .card-top {{ display:flex; justify-content:space-between; align-items:center; }}
  .num {{ color:#656d76; font-size:13px; font-weight:600; }}
  .card h3 {{ font-size:14px; margin:6px 0; line-height:1.35; }}
  .summary {{ font-size:13px; color:#57606a; margin:0 0 10px; max-height:3em; overflow:hidden; }}
  .lasthuman {{ font-size:12px; color:#3b3b6a; background:#f3f0ff; border-left:3px solid #8250df; padding:4px 8px; margin:0 0 8px; border-radius:0 4px 4px 0; max-height:3.4em; overflow:hidden; }}
  .flags {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .pill {{ color:#fff; font-size:11px; padding:2px 8px; border-radius:10px; white-space:nowrap; }}
  .badges {{ display:flex; gap:4px; }}
  .badge {{ font-size:11px; padding:2px 8px; border-radius:10px; }}
  .badge.done {{ background:#dafbe1; color:#1a7f37; }} .badge.stale {{ background:#fff1e5; color:#9a6700; }}
  .badge.reviewed {{ background:#ddf4ff; color:#0969da; }}
  .badge.pending {{ background:#f6f8fa; color:#656d76; border:1px solid #d0d7de; }}
  .dash-only {{ opacity:0.7; background:repeating-linear-gradient(45deg,#f6f8fa,#f6f8fa 10px,#eef1f4 10px,#eef1f4 20px);
                border:1px dashed #adb5bd; border-radius:8px; padding:6px 12px; margin:10px 0; }}
  .dash-only-tag {{ font-size:11px; font-weight:600; color:#6e7781; text-transform:uppercase;
                    letter-spacing:.4px; margin-bottom:4px; }}
  .empty {{ color:#656d76; text-align:center; padding:40px; }}
  /* detail overlay */
  .overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:10; }}
  .overlay.open {{ display:block; }}
  .panel {{ position:absolute; right:0; top:0; bottom:0; width:min(760px,92vw); background:#fff; overflow:auto; box-shadow:-4px 0 20px rgba(0,0,0,.2); }}
  .panel header {{ position:sticky; top:0; background:#f6f8fa; border-bottom:1px solid #d0d7de; padding:14px 20px; display:flex; justify-content:space-between; align-items:center; }}
  .panel header h2 {{ font-size:15px; margin:0; }}
  .hdr-right {{ display:flex; align-items:center; gap:14px; white-space:nowrap; }}
  .gh-link {{ font-size:13px; color:#0969da; text-decoration:none; }}
  .gh-link:hover {{ text-decoration:underline; }}
  .close {{ cursor:pointer; border:none; background:none; font-size:22px; line-height:1; color:#656d76; }}
  .review {{ padding:8px 24px; }}
  .review pre {{ background:#f6f8fa; padding:10px; border-radius:6px; overflow:auto; }}
  .review h1 {{ font-size:19px; }} .review h2 {{ font-size:15px; border-bottom:1px solid #eaeef2; padding-bottom:4px; }}
  .actions {{ padding:14px 24px 28px; border-top:1px solid #d0d7de; background:#fafbfc; }}
  textarea {{ width:100%; min-height:120px; box-sizing:border-box; border:1px solid #d0d7de; border-radius:6px; padding:8px; font:13px/1.5 monospace; }}
  .btns {{ margin-top:8px; display:flex; gap:8px; }}
  button.act {{ cursor:pointer; border:1px solid #d0d7de; border-radius:6px; padding:7px 16px; font:inherit; }}
  .approve {{ background:#1f883d; color:#fff; border-color:#1a7f37; }}
  .request {{ background:#cf222e; color:#fff; border-color:#a40e26; }}
  .comment {{ background:#fff; }}
  .result {{ margin-top:8px; font-size:13px; white-space:pre-wrap; font-family:monospace; }}
  .result.ok {{ color:#1a7f37; }} .result.err {{ color:#cf222e; }}
  .acted-line {{ padding:8px 24px 0; font-size:13px; color:#1a7f37; }}
  .acted-line:empty {{ display:none; }}
  .secbar {{ padding:8px 20px; font-size:13px; border-bottom:1px solid #d0d7de; display:flex; flex-wrap:wrap; align-items:center; gap:8px; }}
  .secbar.alert {{ background:#fff1f0; color:#82071e; }}
  .secbar.ok {{ background:#eaffea; color:#0a5f1a; }}
  .secbar.unavail {{ background:#fff8e1; color:#7a5b00; }}
  .secbar .sechead {{ font-weight:600; }}
  .secbar .pill {{ color:#fff; padding:1px 8px; border-radius:10px; font-size:11px; }}
  .secbar a {{ color:inherit; text-decoration:underline; }}
  .secbar details {{ flex-basis:100%; margin-top:4px; }}
  .secbar summary {{ cursor:pointer; font-size:12px; }}
  .secbar .seclist {{ margin:6px 0 0; padding-left:18px; }}
  .secbar .seclist li {{ margin:2px 0; }}
  .secbar .secpath {{ color:#57606a; font-family:ui-monospace,monospace; font-size:11px; }}
</style></head><body>
<div class="topbar"><h1>CAO PR Triage · {STATE['repo']} · {len(prs)} open</h1><a href="/graph" style="color:#fff;text-decoration:none;font-size:13px;border:1px solid #ffffff55;padding:3px 10px;border-radius:12px;margin-left:12px">🕸 relationship graph</a><span class="mode {mode_cls}">{mode}</span></div>
{sec_banner}
<div class="filterbar">
  <input class="search" id="f-text" type="search" placeholder="search # / title / author…" oninput="applyFilters()">
  <select id="f-urgency" onchange="applyFilters()"><option value="">urgency: any</option><option>high</option><option>medium</option><option>low</option></select>
  <select id="f-ci" onchange="applyFilters()"><option value="">CI: any</option><option>passing</option><option>failing</option><option>pending</option><option value="none">none</option></select>
  <select id="f-verdict" onchange="applyFilters()"><option value="">verdict: any</option><option value="request">request changes</option><option value="approve">approve</option></select>
  <select id="f-status" onchange="applyFilters()"><option value="">review: any</option><option value="reviewed">reviewed</option><option value="pending">pending</option></select>
  <select id="f-acted" onchange="applyFilters()"><option value="">action: any</option><option value="unacted">not acted (at head)</option><option value="acted">acted</option></select>
  <select id="f-pending" onchange="applyFilters()"><option value="">pending: any</option><option value="yes">⚡ pending my decision</option></select>
  <select id="f-attention" onchange="applyFilters()"><option value="">attention: any</option><option value="code">🔁 code changed</option><option value="discussion">💬 discussion</option></select>
  <select id="f-conflicts" onchange="applyFilters()"><option value="">merge: any</option><option value="yes">⚠️ conflicts</option></select>
  <select id="f-human" onchange="applyFilters()"><option value="">human review: any</option><option value="yes">👤 has human reviewer</option></select>
  <select id="f-mismatch" onchange="applyFilters()"><option value="">mismatch: any</option><option value="yes">⚠️ verdict vs human</option></select>
  <select id="f-fit" onchange="applyFilters()"><option value="">fit: any</option><option value="core">🎯 core</option><option value="adjacent">🎯 adjacent</option><option value="scope-creep">🎯 scope-creep</option><option value="off-mission">🎯 off-mission</option></select>
  <button id="f-reset" onclick="resetFilters()">reset</button>
  <span id="f-count"></span>
</div>
<main><div class="grid">{grid}</div></main>

<div class="overlay" id="overlay" onclick="if(event.target===this)closeDetail()">
  <div class="panel">
    <header>
      <h2 id="d-title"></h2>
      <span class="hdr-right">
        <a id="d-link" href="#" target="_blank" rel="noopener" class="gh-link">View on GitHub ↗</a>
        <button class="close" onclick="closeDetail()">×</button>
      </span>
    </header>
    <div id="d-acted" class="acted-line"></div>
    <div class="review" id="d-review"></div>
    <div class="actions">
      <textarea id="d-body"></textarea>
      <div class="btns">
        <button class="act approve" onclick="act('approve')">✓ Approve</button>
        <button class="act comment" onclick="act('comment')">💬 Comment</button>
        <button class="act request" onclick="act('request')">✗ Request changes</button>
      </div>
      <div class="result" id="d-result"></div>
    </div>
  </div>
</div>

<script>
const REPO = {json.dumps(STATE['repo'])};
let CUR = null;
function fmtActed(acted, at) {{
  if (!acted) return '';
  let when = at ? ' on ' + new Date(at).toLocaleString() : '';
  const verb = {{approved:'Approved', commented:'Commented', requested:'Requested changes'}}[acted] || acted;
  return '✓ ' + verb + when;
}}
function openDetail(card) {{
  CUR = {{ pr: card.dataset.pr, sha: card.dataset.sha }};
  document.getElementById('d-title').textContent = '#'+card.dataset.pr+' · '+card.dataset.title;
  document.getElementById('d-link').href = 'https://github.com/'+REPO+'/pull/'+card.dataset.pr;
  document.getElementById('d-review').innerHTML = JSON.parse(card.dataset.html);
  document.getElementById('d-body').value = JSON.parse(card.dataset.raw);
  document.getElementById('d-acted').textContent = fmtActed(JSON.parse(card.dataset.acted||'""'), JSON.parse(card.dataset.actedAt||'""'));
  document.getElementById('d-result').textContent = '';
  document.getElementById('overlay').classList.add('open');
}}
function closeDetail() {{ document.getElementById('overlay').classList.remove('open'); CUR=null; }}
document.addEventListener('keydown', e => {{ if(e.key==='Escape') closeDetail(); }});

// --- client-side filtering (all data is in the card dataset; no server round-trip) ---
const FILTERS = [
  ['f-urgency','fUrgency'], ['f-ci','fCi'], ['f-verdict','fVerdict'],
  ['f-status','fStatus'], ['f-acted','fActed'], ['f-attention','fAttention'],
  ['f-pending','fPending'],
  ['f-conflicts','fConflicts'], ['f-human','fHuman'], ['f-mismatch','fMismatch'], ['f-fit','fFit'],
];
function applyFilters() {{
  const text = document.getElementById('f-text').value.trim().toLowerCase();
  const sel = {{}};
  FILTERS.forEach(([id,ds]) => sel[ds] = document.getElementById(id).value);
  let shown = 0;
  document.querySelectorAll('.card').forEach(card => {{
    let ok = true;
    for (const [id,ds] of FILTERS) {{ if (sel[ds] && card.dataset[ds] !== sel[ds]) {{ ok = false; break; }} }}
    if (ok && text && !(card.dataset.fText||'').includes(text)) ok = false;
    card.classList.toggle('hidden', !ok);
    if (ok) shown++;
  }});
  const total = document.querySelectorAll('.card').length;
  document.getElementById('f-count').textContent = shown + ' / ' + total + ' shown';
}}
function resetFilters() {{
  document.getElementById('f-text').value = '';
  FILTERS.forEach(([id]) => document.getElementById(id).value = '');
  applyFilters();
}}
document.addEventListener('DOMContentLoaded', applyFilters);
async function act(action) {{
  const result = document.getElementById('d-result');
  const body = document.getElementById('d-body').value;
  if (action !== 'approve' && !body.trim()) {{ result.className='result err'; result.textContent='A body is required for '+action; return; }}
  result.className='result'; result.textContent='Running…';
  try {{
    const r = await fetch('/api/action', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{pr:CUR.pr, sha:CUR.sha, action, body}})}});
    const d = await r.json();
    result.className = 'result ' + (d.ok ? 'ok' : 'err');
    result.textContent = d.output;
    if (d.ok) {{
      // reflect the action + timestamp immediately, without a page reload
      const rec = {{approve:'approved', comment:'commented', request:'requested'}}[action] || action;
      document.getElementById('d-acted').textContent = fmtActed(rec, new Date().toISOString());
    }}
  }} catch(e) {{ result.className='result err'; result.textContent=String(e); }}
}}
</script></body></html>"""


_GRAPH_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>CAO PR Graph — __REPO__</title>
<style>
  html,body { margin:0; height:100%; font:14px/1.4 -apple-system,system-ui,sans-serif; background:#0d1117; color:#e6edf3; overflow:hidden; }
  .topbar { position:fixed; top:0; left:0; right:0; height:44px; background:#161b22; border-bottom:1px solid #30363d; display:flex; align-items:center; gap:16px; padding:0 16px; z-index:10; }
  .topbar h1 { font-size:15px; margin:0; font-weight:600; }
  .topbar a { color:#58a6ff; text-decoration:none; font-size:13px; }
  .topbar .meta { color:#8b949e; font-size:12px; margin-left:auto; }
  .panel { position:fixed; top:56px; left:12px; width:230px; background:#161b22ee; border:1px solid #30363d; border-radius:8px; padding:12px 14px; z-index:9; font-size:12.5px; max-height:calc(100vh - 72px); overflow:auto; }
  .panel h3 { margin:2px 0 8px; font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:#8b949e; }
  .panel label { display:flex; align-items:center; gap:7px; padding:3px 0; cursor:pointer; }
  .panel .sect { margin-bottom:12px; border-bottom:1px solid #21262d; padding-bottom:10px; }
  .panel input[type=search] { width:100%; box-sizing:border-box; background:#0d1117; border:1px solid #30363d; color:#e6edf3; border-radius:6px; padding:5px 8px; }
  .legrow { display:flex; align-items:center; gap:8px; padding:2px 0; }
  .dot { width:12px; height:12px; border-radius:50%; flex:none; }
  .dia { width:11px; height:11px; flex:none; transform:rotate(45deg); border:2px solid #e3a008; }
  .ln { width:20px; height:0; flex:none; border-top-width:2px; border-top-style:solid; }
  svg { position:fixed; inset:0; width:100vw; height:100vh; }
  #tip { position:fixed; pointer-events:none; background:#161b22; border:1px solid #30363d; border-radius:6px; padding:8px 10px; font-size:12px; max-width:320px; z-index:20; display:none; box-shadow:0 4px 16px #0008; }
  #tip b { color:#58a6ff; }
  #tip .k { color:#8b949e; }
  .node circle, .node polygon { cursor:pointer; stroke:#0d1117; stroke-width:1.5; }
  .node text { font-size:9px; fill:#c9d1d9; pointer-events:none; text-anchor:middle; }
  .hint { color:#8b949e; font-size:11px; margin-top:6px; }
  .empty { position:fixed; inset:0; display:flex; align-items:center; justify-content:center; color:#8b949e; text-align:center; padding:40px; }
</style></head><body>
<div class="topbar">
  <h1>CAO PR Relationship Graph</h1>
  <a href="/">← back to triage board</a>
  <span class="meta" id="meta"></span>
</div>
<div class="panel">
  <div class="sect">
    <h3>Edges</h3>
    <label><input type="checkbox" id="e-reference" checked> references (#N)</label>
    <label><input type="checkbox" id="e-issue" checked> issue links</label>
    <label><input type="checkbox" id="e-file-overlap"> file overlap</label>
  </div>
  <div class="sect">
    <h3>Nodes</h3>
    <label><input type="checkbox" id="n-open" checked> open PRs</label>
    <label><input type="checkbox" id="n-merged" checked> merged PRs</label>
    <label><input type="checkbox" id="n-closed" checked> closed PRs</label>
    <label><input type="checkbox" id="n-issue" checked> issues</label>
    <label><input type="checkbox" id="only-connected"> hide isolated nodes</label>
  </div>
  <div class="sect">
    <h3>Open-PR class</h3>
    <div class="legrow"><span class="dot" style="background:#a371f7"></span> follow-up</div>
    <div class="legrow"><span class="dot" style="background:#58a6ff"></span> builds-on</div>
    <div class="legrow"><span class="dot" style="background:#3fb950"></span> related</div>
    <div class="legrow"><span class="dot" style="background:#d29922"></span> new</div>
    <div class="legrow"><span class="dot" style="background:#6e7681"></span> merged</div>
    <div class="legrow"><span class="dot" style="background:#f85149"></span> closed</div>
    <div class="legrow"><span class="dia"></span> issue</div>
  </div>
  <div class="sect">
    <h3>Edge types</h3>
    <div class="legrow"><span class="ln" style="border-color:#a371f7"></span> follow-up/supersedes</div>
    <div class="legrow"><span class="ln" style="border-color:#3fb950"></span> fixes/closes</div>
    <div class="legrow"><span class="ln" style="border-color:#8b949e"></span> refs/mentions</div>
    <div class="legrow"><span class="ln" style="border-color:#e3a008;border-top-style:dashed"></span> issue link</div>
    <div class="legrow"><span class="ln" style="border-color:#30474d"></span> file overlap</div>
  </div>
  <input type="search" id="search" placeholder="highlight #num or title…">
  <div class="hint">drag nodes · scroll to zoom · drag bg to pan · click = open on GitHub</div>
</div>
<svg id="svg"><g id="vp"><g id="edges"></g><g id="nodes"></g></g>
  <defs>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#8b949e"/></marker>
  </defs>
</svg>
<div id="tip"></div>
<script>
const REPO = "__REPO__";
const svg = document.getElementById('svg'), vp = document.getElementById('vp');
const gEdges = document.getElementById('edges'), gNodes = document.getElementById('nodes');
const tip = document.getElementById('tip');
const CLS = {'follow-up':'#a371f7','builds-on':'#58a6ff','related':'#3fb950','new':'#d29922'};
const EK = {'followup':'#a371f7','supersedes':'#a371f7','buildson':'#a371f7','partof':'#a371f7',
            'fixes':'#3fb950','closes':'#3fb950','refs':'#8b949e','mentions':'#8b949e'};
let nodes=[], edges=[], byId={}, view={tx:innerWidth/2,ty:innerHeight/2,s:0.8}, alpha=1, dragging=null, panning=null;

function nodeColor(n){
  if(n.type==='issue') return '#e3a008';
  if(n.state==='OPEN') return CLS[n.classification]||'#8b949e';
  if(n.state==='MERGED') return '#6e7681';
  return '#f85149';
}
function nodeR(n){ if(n.type==='issue') return 6; return 6 + Math.min(10, (n.n_files||0)*0.5) + (n.open?2:0); }
function edgeColor(e){ if(e.type==='file-overlap') return '#30474d'; if(e.type==='issue') return '#e3a008'; return EK[e.kind]||'#8b949e'; }

fetch('/api/graph').then(r=>r.json()).then(g=>{
  if(g.error || !g.nodes || !g.nodes.length){
    document.body.insertAdjacentHTML('beforeend','<div class="empty">'+(g.error||'No graph data.')+'<br><br>Run <code>examples/pr-review/build_pr_graph.sh</code> then reload.</div>'); return;
  }
  nodes = g.nodes; edges = g.edges;
  document.getElementById('meta').textContent =
    (g.counts.prs||0)+' PRs · '+(g.counts.issues||0)+' issues · '+(edges.length)+' edges · '+(g.generated_at||'');
  const R=Math.min(innerWidth,innerHeight)*0.42;
  nodes.forEach((n,i)=>{ n.key=String(n.id); byId[n.key]=n;
    const a=i*2.399; n.x=Math.cos(a)*R*Math.random(); n.y=Math.sin(a)*R*Math.random(); n.vx=0; n.vy=0; n.deg=0; });
  edges.forEach(e=>{ e.s=byId[String(e.source)]; e.t=byId[String(e.target)];
    if(e.s&&e.t){ e.s.deg++; e.t.deg++; } });
  edges = edges.filter(e=>e.s&&e.t);
  build(); requestAnimationFrame(tick);
  ['e-reference','e-issue','e-file-overlap','n-open','n-merged','n-closed','n-issue','only-connected']
    .forEach(id=>document.getElementById(id).addEventListener('change',applyFilters));
  document.getElementById('search').addEventListener('input',applyFilters);
  applyFilters();
});

let lineEls=[], nodeEls=[];
function build(){
  gEdges.innerHTML=''; gNodes.innerHTML=''; lineEls=[]; nodeEls=[];
  edges.forEach(e=>{ const l=document.createElementNS('http://www.w3.org/2000/svg','line');
    l.setAttribute('stroke',edgeColor(e));
    l.setAttribute('stroke-width', e.type==='file-overlap'?Math.min(3,0.5+(e.weight||1)/8):1.6);
    l.setAttribute('stroke-opacity', e.type==='file-overlap'?0.28:0.7);
    if(e.type==='issue') l.setAttribute('stroke-dasharray','4 3');
    if(e.type==='reference') l.setAttribute('marker-end','url(#arrow)');
    e.el=l; gEdges.appendChild(l); lineEls.push(l); });
  nodes.forEach(n=>{ const g=document.createElementNS('http://www.w3.org/2000/svg','g'); g.setAttribute('class','node');
    let shape;
    if(n.type==='issue'){ shape=document.createElementNS('http://www.w3.org/2000/svg','rect');
      const s=9; shape.setAttribute('width',s); shape.setAttribute('height',s); shape.setAttribute('x',-s/2); shape.setAttribute('y',-s/2);
      shape.setAttribute('transform','rotate(45)'); }
    else { shape=document.createElementNS('http://www.w3.org/2000/svg','circle'); shape.setAttribute('r',nodeR(n)); }
    shape.setAttribute('fill',nodeColor(n));
    if(n.draft) shape.setAttribute('stroke-dasharray','2 2');
    g.appendChild(shape);
    const t=document.createElementNS('http://www.w3.org/2000/svg','text'); t.setAttribute('dy',nodeR(n)+9);
    t.textContent=(n.type==='issue'?'#'+n.number:'#'+n.id); g.appendChild(t);
    n.el=g; n.shape=shape;
    g.addEventListener('mousedown',ev=>startDrag(ev,n));
    g.addEventListener('mouseenter',ev=>showTip(ev,n));
    g.addEventListener('mousemove',ev=>{tip.style.left=(ev.clientX+14)+'px';tip.style.top=(ev.clientY+14)+'px';});
    g.addEventListener('mouseleave',()=>tip.style.display='none');
    g.addEventListener('click',ev=>{ if(n._moved){n._moved=false;return;}
      const kind=n.type==='issue'?'issues':'pull'; window.open('https://github.com/'+REPO+'/'+kind+'/'+(n.number||n.id),'_blank'); });
    gNodes.appendChild(g); nodeEls.push(g); });
}

function tick(){
  if(alpha>0.005){
    const k=alpha;
    for(let i=0;i<nodes.length;i++){ const a=nodes[i];
      for(let j=i+1;j<nodes.length;j++){ const b=nodes[j];
        let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy||0.01; if(d2>90000) continue;
        const f=1400/d2; const d=Math.sqrt(d2); const fx=dx/d*f, fy=dy/d*f;
        a.vx+=fx*k; a.vy+=fy*k; b.vx-=fx*k; b.vy-=fy*k; } }
    edges.forEach(e=>{ const a=e.s,b=e.t; let dx=b.x-a.x, dy=b.y-a.y, d=Math.sqrt(dx*dx+dy*dy)||0.01;
      const L = e.type==='file-overlap'?150:90; const str=(e.type==='file-overlap'?0.005:0.03);
      const f=(d-L)*str*k; const fx=dx/d*f, fy=dy/d*f; a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy; });
    nodes.forEach(n=>{ n.vx+=(-n.x)*0.002*k; n.vy+=(-n.y)*0.002*k;
      if(n===dragging) return;
      n.x+=n.vx*=0.85; n.y+=n.vy*=0.85; });
    alpha*=0.992;
  }
  vp.setAttribute('transform','translate('+view.tx+','+view.ty+') scale('+view.s+')');
  lineEls.forEach((l,i)=>{ const e=edges[i]; l.setAttribute('x1',e.s.x); l.setAttribute('y1',e.s.y); l.setAttribute('x2',e.t.x); l.setAttribute('y2',e.t.y); });
  nodeEls.forEach((g,i)=>{ const n=nodes[i]; g.setAttribute('transform','translate('+n.x+','+n.y+')'); });
  requestAnimationFrame(tick);
}

function applyFilters(){
  const on={reference:document.getElementById('e-reference').checked, issue:document.getElementById('e-issue').checked, 'file-overlap':document.getElementById('e-file-overlap').checked};
  const ns={OPEN:document.getElementById('n-open').checked, MERGED:document.getElementById('n-merged').checked, CLOSED:document.getElementById('n-closed').checked};
  const showIssue=document.getElementById('n-issue').checked;
  const onlyConn=document.getElementById('only-connected').checked;
  const q=document.getElementById('search').value.trim().toLowerCase();
  const vis={};
  nodes.forEach(n=>{ let v = n.type==='issue'?showIssue:(ns[n.state]!==false);
    n._vis=v; vis[n.key]=v; });
  // edge visibility depends on its type toggle AND both endpoints visible
  const conn={};
  edges.forEach(e=>{ const v = on[e.type] && vis[e.s.key] && vis[e.t.key];
    e.el.style.display=v?'':'none'; if(v){conn[e.s.key]=1;conn[e.t.key]=1;} });
  nodes.forEach(n=>{ let v=n._vis; if(v&&onlyConn&&!conn[n.key]) v=false;
    let dim=false; if(q){ const hay=('#'+ (n.number||n.id)+' '+(n.title||'')).toLowerCase(); dim = !hay.includes(q); }
    n.el.style.display=v?'':'none'; n.el.style.opacity=(v&&dim)?0.15:1; });
  alpha=Math.max(alpha,0.3);
}

function showTip(ev,n){
  let h;
  if(n.type==='issue'){ h='<b>issue #'+n.number+'</b>'; }
  else { h='<b>PR #'+n.id+'</b> <span class="k">'+n.state+(n.draft?' · draft':'')+'</span><br>'+
    (n.title||'').replace(/</g,'&lt;')+'<br>'+
    '<span class="k">@'+(n.author||'?')+(n.classification?' · '+n.classification:'')+(n.verdict?' · '+n.verdict:'')+'</span>'+
    (n.n_files?'<br><span class="k">'+n.n_files+' files</span>':'')+
    (Object.keys(n.pr_refs||{}).length?'<br><span class="k">→ refs PR '+Object.entries(n.pr_refs).map(x=>'#'+x[0]+'('+x[1]+')').join(', ')+'</span>':'')+
    (Object.keys(n.issue_refs||{}).length?'<br><span class="k">→ issue '+Object.keys(n.issue_refs).map(x=>'#'+x).join(', ')+'</span>':''); }
  tip.innerHTML=h; tip.style.display='block'; tip.style.left=(ev.clientX+14)+'px'; tip.style.top=(ev.clientY+14)+'px';
}

// ---- interaction: node drag, background pan, zoom ----
function toWorld(px,py){ return {x:(px-view.tx)/view.s, y:(py-view.ty)/view.s}; }
function startDrag(ev,n){ ev.stopPropagation(); dragging=n; n._down=[ev.clientX,ev.clientY]; alpha=Math.max(alpha,0.5); }
svg.addEventListener('mousedown',ev=>{ if(!dragging){ panning=[ev.clientX,ev.clientY,view.tx,view.ty]; } });
window.addEventListener('mousemove',ev=>{
  if(dragging){ const w=toWorld(ev.clientX,ev.clientY); dragging.x=w.x; dragging.y=w.y; dragging.vx=0; dragging.vy=0;
    if(dragging._down && (Math.abs(ev.clientX-dragging._down[0])>3||Math.abs(ev.clientY-dragging._down[1])>3)) dragging._moved=true; }
  else if(panning){ view.tx=panning[2]+(ev.clientX-panning[0]); view.ty=panning[3]+(ev.clientY-panning[1]); }
});
window.addEventListener('mouseup',()=>{ dragging=null; panning=null; });
svg.addEventListener('wheel',ev=>{ ev.preventDefault(); const f=ev.deltaY<0?1.1:0.9;
  const wx=(ev.clientX-view.tx)/view.s, wy=(ev.clientY-view.ty)/view.s;
  view.s=Math.max(0.15,Math.min(4,view.s*f)); view.tx=ev.clientX-wx*view.s; view.ty=ev.clientY-wy*view.s; },{passive:false});
</script>
</body></html>"""


def render_graph_page() -> str:
    repo = STATE["repo"] or "awslabs/cli-agent-orchestrator"
    return _GRAPH_HTML.replace("__REPO__", html.escape(repo))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="awslabs/cli-agent-orchestrator")
    ap.add_argument("--data-dir", default="pr-review-data",
                    help="Where the manager/supervisor write meta/, reviews/, state.json. "
                         "Relative to the repo root (the agents' working directory).")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--execute", action="store_true",
                    help="Actually run gh (post comments/approvals). Default is dry-run.")
    args = ap.parse_args()
    STATE["repo"] = args.repo
    STATE["data_dir"] = Path(args.data_dir)
    STATE["execute"] = args.execute
    (data() / "reviews").mkdir(parents=True, exist_ok=True)
    (data() / "meta").mkdir(parents=True, exist_ok=True)
    print(f"Dashboard: http://localhost:{args.port}  repo={args.repo}  "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'}  data={args.data_dir}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
