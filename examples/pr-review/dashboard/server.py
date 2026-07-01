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
import subprocess
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import markdown as md
import uvicorn

STATE = {"repo": "", "data_dir": Path("."), "execute": False}

URGENCY_RANK = {"high": 0, "medium": 1, "low": 2, "": 3}
URGENCY_COLOR = {"high": "#cf222e", "medium": "#9a6700", "low": "#1a7f37"}
IMPORTANCE_COLOR = {"high": "#8250df", "medium": "#0969da", "low": "#656d76"}


def data() -> Path:
    return STATE["data_dir"]


def load_state() -> dict:
    f = data() / "state.json"
    return json.loads(f.read_text()) if f.exists() else {}


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
    metas, reviews = {}, {}
    meta_dir = data() / "meta"
    if meta_dir.exists():
        for mf in meta_dir.glob("*.json"):
            pr, _, sha = mf.stem.partition("-")
            if pr.isdigit():
                try:
                    metas[pr] = (sha, json.loads(mf.read_text()))
                except json.JSONDecodeError:
                    pass
    for f in sorted((data() / "reviews").glob("*.md")):
        pr, _, sha = f.stem.partition("-")
        if pr.isdigit():
            reviews[pr] = (sha, *parse_frontmatter(f.read_text()))  # (sha, frontmatter, body)

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
            # review body + action state
            "html": md.markdown(body, extensions=["fenced_code", "tables"]) if body
                    else "<p><em>Deep review pending — metadata only so far.</em></p>",
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


@app.post("/api/action")
async def api_action(req: Request) -> JSONResponse:
    d = await req.json()
    pr, sha, action, body = str(d["pr"]), str(d.get("sha", "")), d["action"], d.get("body", "")
    repo = STATE["repo"]
    if action == "approve":
        res = run_gh(["pr", "review", pr, "--repo", repo, "--approve",
                      "--body", body or "Approved after multi-angle CAO review."])
        recorded = "approved"
    elif action == "request":
        res = run_gh(["pr", "review", pr, "--repo", repo, "--request-changes",
                      "--body", body or "Changes requested — see review."])
        recorded = "requested"
    elif action == "comment":
        res = run_gh(["pr", "comment", pr, "--repo", repo, "--body", body])
        recorded = "commented"
    else:
        return JSONResponse({"ok": False, "output": f"unknown action {action}"}, status_code=400)
    if res["ok"]:
        record_action(pr, sha, recorded)
    return JSONResponse(res)


def pill(text: str, color: str) -> str:
    return f'<span class="pill" style="background:{color}">{html.escape(str(text))}</span>'


def render_page(prs: list[dict]) -> str:
    mode = "EXECUTE — actions hit GitHub" if STATE["execute"] else "DRY-RUN — actions are simulated"
    mode_cls = "exec" if STATE["execute"] else "dry"
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
                 data-f-acted="{'acted' if r['acted'] else 'unacted'}"
                 data-f-attention="{'code' if r['code_changed'] else 'discussion' if r['human_activity'] else 'none'}"
                 data-f-text="{html.escape((str(r['pr']) + ' ' + (r['title'] or '') + ' ' + (r['author'] or '')).lower())}"
                 onclick="openDetail(this)">
          <div class="card-top"><span class="num">#{r['pr']}</span><span class="badges">{review_badge}{acted_badge}</span></div>
          <h3>{html.escape(r['title'])}</h3>
          <p class="summary">{html.escape(r['summary'] or r['verdict'] or '')}</p>
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
  .flags {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .pill {{ color:#fff; font-size:11px; padding:2px 8px; border-radius:10px; white-space:nowrap; }}
  .badges {{ display:flex; gap:4px; }}
  .badge {{ font-size:11px; padding:2px 8px; border-radius:10px; }}
  .badge.done {{ background:#dafbe1; color:#1a7f37; }} .badge.stale {{ background:#fff1e5; color:#9a6700; }}
  .badge.reviewed {{ background:#ddf4ff; color:#0969da; }}
  .badge.pending {{ background:#f6f8fa; color:#656d76; border:1px solid #d0d7de; }}
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
</style></head><body>
<div class="topbar"><h1>CAO PR Triage · {STATE['repo']} · {len(prs)} open</h1><span class="mode {mode_cls}">{mode}</span></div>
<div class="filterbar">
  <input class="search" id="f-text" type="search" placeholder="search # / title / author…" oninput="applyFilters()">
  <select id="f-urgency" onchange="applyFilters()"><option value="">urgency: any</option><option>high</option><option>medium</option><option>low</option></select>
  <select id="f-ci" onchange="applyFilters()"><option value="">CI: any</option><option>passing</option><option>failing</option><option>pending</option><option value="none">none</option></select>
  <select id="f-verdict" onchange="applyFilters()"><option value="">verdict: any</option><option value="request">request changes</option><option value="approve">approve</option></select>
  <select id="f-status" onchange="applyFilters()"><option value="">review: any</option><option value="reviewed">reviewed</option><option value="pending">pending</option></select>
  <select id="f-acted" onchange="applyFilters()"><option value="">action: any</option><option value="unacted">not acted</option><option value="acted">acted</option></select>
  <select id="f-attention" onchange="applyFilters()"><option value="">attention: any</option><option value="code">🔁 code changed</option><option value="discussion">💬 discussion</option></select>
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
