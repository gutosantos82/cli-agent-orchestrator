#!/usr/bin/env python3
"""PR Review Dashboard — a local web UI to eyeball CAO PR reviews and act on them.

The pr_review_manager agent writes one markdown report per reviewed PR into
`<data-dir>/reviews/<pr>-<sha>.md` and tracks state in `<data-dir>/state.json`.
This server renders those reviews and exposes live Approve / Comment / Request-changes
buttons that shell out to `gh`.

Run:
    uv run --with fastapi --with uvicorn --with markdown \
        examples/pr-review/dashboard/server.py --repo awslabs/cli-agent-orchestrator

Safety:
    --dry-run   (default ON) prints the gh command instead of executing it.
    Pass --execute to actually post comments / approvals to GitHub.
"""
import argparse
import json
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import markdown as md
import uvicorn

# --- config (set in main, read by handlers) ---------------------------------
STATE = {"repo": "", "data_dir": Path("."), "execute": False}


def reviews_dir() -> Path:
    return STATE["data_dir"] / "reviews"


def load_state() -> dict:
    f = STATE["data_dir"] / "state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {}


def list_reviews() -> list[dict]:
    """One entry per PR, newest review file wins. Cross-referenced with state.json."""
    state = load_state()
    out = {}
    for f in sorted(reviews_dir().glob("*.md")):
        # filename: <pr>-<sha>.md
        stem = f.stem
        pr, _, sha = stem.partition("-")
        if not pr.isdigit():
            continue
        body = f.read_text()
        title = body.splitlines()[0].lstrip("# ").strip() if body else f"PR #{pr}"
        st = state.get(pr, {})
        out[pr] = {
            "pr": pr,
            "sha": sha,
            "title": title,
            "html": md.markdown(body, extensions=["fenced_code", "tables"]),
            "acted": st.get("acted"),           # None | "approved" | "commented" | "requested"
            "acted_sha": st.get("acted_sha"),   # SHA at which the last action was taken
            "stale": bool(st.get("acted_sha") and st.get("acted_sha") != sha),
        }
    # newest first by PR number
    return [out[k] for k in sorted(out, key=lambda x: int(x), reverse=True)]


def run_gh(args: list[str]) -> dict:
    """Run a gh command (or echo it in dry-run). Returns {ok, output}."""
    cmd = ["gh", *args]
    if not STATE["execute"]:
        return {"ok": True, "output": f"[DRY-RUN] would run: {' '.join(cmd)}"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        ok = r.returncode == 0
        return {"ok": ok, "output": (r.stdout + r.stderr).strip() or ("done" if ok else "failed")}
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        return {"ok": False, "output": str(e)}


def record_action(pr: str, sha: str, action: str) -> None:
    f = STATE["data_dir"] / "state.json"
    state = load_state()
    entry = state.get(pr, {})
    entry.update({"acted": action, "acted_sha": sha})
    state[pr] = entry
    f.write_text(json.dumps(state, indent=2))


app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render_page(list_reviews())


@app.get("/api/reviews")
def api_reviews() -> JSONResponse:
    return JSONResponse(list_reviews())


@app.post("/api/action")
async def api_action(req: Request) -> JSONResponse:
    data = await req.json()
    pr = str(data["pr"])
    sha = str(data.get("sha", ""))
    action = data["action"]            # approve | comment | request
    body = data.get("body", "")
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


def render_page(reviews: list[dict]) -> str:
    mode = "EXECUTE — actions hit GitHub" if STATE["execute"] else "DRY-RUN — actions are simulated"
    mode_cls = "exec" if STATE["execute"] else "dry"
    cards = []
    for r in reviews:
        badge = ""
        if r["acted"]:
            cls = "stale" if r["stale"] else "done"
            extra = " (stale — PR changed since)" if r["stale"] else ""
            badge = f'<span class="badge {cls}">{r["acted"]}{extra}</span>'
        cards.append(f"""
        <section class="card" data-pr="{r['pr']}" data-sha="{r['sha']}">
          <header>
            <h2>PR #{r['pr']} {badge}</h2>
            <code class="sha">{r['sha'][:10]}</code>
          </header>
          <div class="review">{r['html']}</div>
          <div class="actions">
            <textarea placeholder="Optional comment / review body…"></textarea>
            <div class="btns">
              <button class="approve" onclick="act(this,'approve')">✓ Approve</button>
              <button class="comment" onclick="act(this,'comment')">💬 Comment</button>
              <button class="request" onclick="act(this,'request')">✗ Request changes</button>
            </div>
            <div class="result"></div>
          </div>
        </section>""")
    body = "\n".join(cards) or "<p>No reviews yet. Run the pr_review_manager.</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>CAO PR Reviews — {STATE['repo']}</title>
<style>
  body {{ font: 15px/1.5 -apple-system,system-ui,sans-serif; margin:0; background:#f6f8fa; color:#1f2328; }}
  .topbar {{ position:sticky; top:0; background:#24292f; color:#fff; padding:12px 20px; display:flex; justify-content:space-between; align-items:center; }}
  .topbar h1 {{ font-size:16px; margin:0; }}
  .mode {{ font-size:12px; padding:3px 10px; border-radius:12px; }}
  .mode.dry {{ background:#9a6700; }} .mode.exec {{ background:#cf222e; }}
  main {{ max-width:900px; margin:20px auto; padding:0 16px; }}
  .card {{ background:#fff; border:1px solid #d0d7de; border-radius:8px; margin-bottom:20px; overflow:hidden; }}
  .card header {{ display:flex; justify-content:space-between; align-items:center; padding:10px 16px; border-bottom:1px solid #d0d7de; background:#f6f8fa; }}
  .card h2 {{ font-size:15px; margin:0; }} .sha {{ color:#656d76; font-size:12px; }}
  .review {{ padding:8px 20px; max-height:420px; overflow:auto; }}
  .review pre {{ background:#f6f8fa; padding:10px; border-radius:6px; overflow:auto; }}
  .review h1 {{ font-size:18px; }} .review h2 {{ font-size:15px; border-bottom:1px solid #eaeef2; padding-bottom:4px; }}
  .actions {{ padding:12px 16px; border-top:1px solid #d0d7de; background:#fafbfc; }}
  textarea {{ width:100%; min-height:48px; box-sizing:border-box; border:1px solid #d0d7de; border-radius:6px; padding:8px; font:inherit; }}
  .btns {{ margin-top:8px; display:flex; gap:8px; }}
  button {{ cursor:pointer; border:1px solid #d0d7de; border-radius:6px; padding:6px 14px; font:inherit; }}
  .approve {{ background:#1f883d; color:#fff; border-color:#1a7f37; }}
  .request {{ background:#cf222e; color:#fff; border-color:#a40e26; }}
  .comment {{ background:#fff; }}
  .result {{ margin-top:8px; font-size:13px; white-space:pre-wrap; font-family:monospace; }}
  .result.ok {{ color:#1a7f37; }} .result.err {{ color:#cf222e; }}
  .badge {{ font-size:11px; padding:2px 8px; border-radius:10px; margin-left:8px; vertical-align:middle; }}
  .badge.done {{ background:#dafbe1; color:#1a7f37; }} .badge.stale {{ background:#fff1e5; color:#9a6700; }}
</style></head><body>
<div class="topbar"><h1>CAO PR Reviews · {STATE['repo']}</h1><span class="mode {mode_cls}">{mode}</span></div>
<main>{body}</main>
<script>
async function act(btn, action) {{
  const card = btn.closest('.card');
  const result = card.querySelector('.result');
  const body = card.querySelector('textarea').value;
  if (action !== 'approve' && !body.trim()) {{ result.className='result err'; result.textContent='A body is required for '+action; return; }}
  result.className='result'; result.textContent='Running…';
  card.querySelectorAll('button').forEach(b=>b.disabled=true);
  try {{
    const r = await fetch('/api/action', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{pr: card.dataset.pr, sha: card.dataset.sha, action, body}})}});
    const d = await r.json();
    result.className = 'result ' + (d.ok ? 'ok' : 'err');
    result.textContent = d.output;
  }} catch(e) {{ result.className='result err'; result.textContent=String(e); }}
  card.querySelectorAll('button').forEach(b=>b.disabled=false);
}}
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="awslabs/cli-agent-orchestrator")
    ap.add_argument("--data-dir", default="pr-review-data",
                    help="Where the manager/supervisor write reviews + state.json. "
                         "Relative to the repo root (the agents' working directory).")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--execute", action="store_true",
                    help="Actually run gh (post comments/approvals). Default is dry-run.")
    args = ap.parse_args()
    STATE["repo"] = args.repo
    STATE["data_dir"] = Path(args.data_dir)
    STATE["execute"] = args.execute
    reviews_dir().mkdir(parents=True, exist_ok=True)
    print(f"Dashboard: http://localhost:{args.port}  repo={args.repo}  "
          f"mode={'EXECUTE' if args.execute else 'DRY-RUN'}  data={args.data_dir}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
