#!/usr/bin/env python3
"""Telegram listener for the CAO pr-review decision buttons.

Long-polls getUpdates (no public webhook needed — works behind NAT) and, on a
tap of the Approve/Comment/Skip buttons sent by notify_telegram_decision.sh,
performs the action via publish_reviews.sh, then edits the message with the
outcome and removes the buttons.

Safety:
  * Only callbacks from the authorized TELEGRAM_CHAT_ID are honored.
  * Only callback_data of the form "prr:<pr>:<action>" is acted on.
  * Every action goes through publish_reviews.sh, which re-checks the CURRENT
    head — so a tap on a stale message (author pushed since) is reported as
    "head moved" and NOT posted.

Actions:
  approve -> publish_reviews.sh --ack <pr> <pr>   (tap == consent; posts approve)
  comment -> publish_reviews.sh --as-comment <pr> (posts review body as comment)
  skip    -> record acted=skipped in state.json (no GitHub write)

Creds: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from env or
~/.aws/cli-agent-orchestrator/pr-review-telegram.env (override CAO_PRR_TELEGRAM_ENV).
Run: python3 examples/pr-review/pr-review-bot.py   (leave running; detach with nohup)
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "pr-review-data"
OFFSET_FILE = DATA_DIR / ".telegram_offset"
PUBLISH = str(REPO_ROOT / "examples/pr-review/publish_reviews.sh")
REPO = os.environ.get("CAO_PRR_REPO", "awslabs/cli-agent-orchestrator")


def current_head(pr):
    try:
        out = subprocess.run(
            ["gh", "pr", "view", pr, "--repo", REPO, "--json", "headRefOid", "--jq", ".headRefOid"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def current_verdict(pr):
    """(verdict_str_or_None, head). None verdict = no report at current head."""
    head = current_head(pr)
    if not head:
        return None, ""
    f = DATA_DIR / "reviews" / f"{pr}-{head}.md"
    if not f.is_file():
        return None, head
    infm = False
    fences = 0
    for line in f.read_text().splitlines():
        if line.strip() == "---":
            fences += 1
            infm = fences == 1
            if fences >= 2:
                break
            continue
        if infm and line.startswith("verdict:"):
            return line.split(":", 1)[1].strip().strip("\"'"), head
    return "", head


def verdict_code(v):
    if not v:
        return None
    if "Request changes" in v:
        return "r"
    if "Approve" in v:
        return "a"
    return "c"


def _ssl_ctx():
    """Trust the system CA bundle (curl trusts it; it carries the corp
    TLS-intercepting root that Python's default bundle lacks)."""
    for p in (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    ):
        if p and Path(p).is_file():
            try:
                return ssl.create_default_context(cafile=p)
            except Exception:  # noqa: BLE001
                continue
    return ssl.create_default_context()


SSL_CTX = _ssl_ctx()


def load_creds():
    cfg = os.environ.get(
        "CAO_PRR_TELEGRAM_ENV",
        str(Path.home() / ".aws/cli-agent-orchestrator/pr-review-telegram.env"),
    )
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    p = Path(cfg)
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN=") and not token:
                token = line.split("=", 1)[1].strip().strip("\"'")
            elif line.startswith("TELEGRAM_CHAT_ID=") and not chat:
                chat = line.split("=", 1)[1].strip().strip("\"'")
    if not token or not chat:
        sys.exit("[bot] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — cannot start")
    return token, str(chat)


TOKEN, CHAT_ID = load_creds()
API = f"https://api.telegram.org/bot{TOKEN}"


def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=60, context=SSL_CTX) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        print(f"[bot] api {method} error: {e}", file=sys.stderr)
        return {}


def read_offset():
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:  # noqa: BLE001
        return 0


def write_offset(v):
    try:
        OFFSET_FILE.write_text(str(v))
    except Exception as e:  # noqa: BLE001
        print(f"[bot] offset write error: {e}", file=sys.stderr)


def record_skip(pr):
    f = DATA_DIR / "state.json"
    try:
        st = json.loads(f.read_text()) if f.is_file() else {}
    except Exception:  # noqa: BLE001
        st = {}
    entry = st.get(str(pr), {})
    entry.update(acted="skipped", acted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    st[str(pr)] = entry
    f.write_text(json.dumps(st, indent=2))


def run_publish(args):
    """Run publish_reviews.sh; return (ok, human_outcome)."""
    try:
        out = subprocess.run(
            ["bash", PUBLISH, *args], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"error running publish: {e}"
    blob = out.stdout + out.stderr
    if "POSTED (approved)" in blob:
        return True, "✅ Approved & posted"
    if "POSTED (requested)" in blob:
        return True, "🔁 Request-changes posted"
    if "POSTED (commented)" in blob:
        return True, "💬 Posted as comment"
    if "SKIP: no report at current head" in blob:
        return False, "⚠️ Head moved since review — not posted; will be re-reviewed"
    if "HOLD:" in blob:
        return False, "held by guard (unexpected)"
    if "FAILED" in blob:
        return False, "❌ Post failed"
    return False, "no action taken (" + (blob.strip().splitlines()[-1] if blob.strip() else "no output") + ")"


def handle_callback(cq):
    cq_id = cq.get("id")
    msg = cq.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    data = cq.get("data", "")
    # Authorization: only the configured chat may drive actions.
    if chat_id != CHAT_ID:
        api("answerCallbackQuery", callback_query_id=cq_id, text="Unauthorized")
        print(f"[bot] rejected callback from chat {chat_id}", file=sys.stderr)
        return
    if not data.startswith("prr:"):
        api("answerCallbackQuery", callback_query_id=cq_id)
        return
    parts = data.split(":")
    if len(parts) < 3:
        api("answerCallbackQuery", callback_query_id=cq_id, text="bad data")
        return
    pr, action = parts[1], parts[2]
    expected = parts[3] if len(parts) > 3 else ""
    print(f"[bot] callback pr=#{pr} action={action} expected={expected}", file=sys.stderr)

    if action == "post":
        # Confirm-the-verdict: re-check the CURRENT head's verdict and post it
        # only if it still matches what the message showed (a=approve, r=request).
        verdict, head = current_verdict(pr)
        vc = verdict_code(verdict)
        if verdict is None:
            ok, outcome = False, "⚠️ head moved / no current report — will be re-reviewed"
        elif vc != expected:
            shown = {"a": "Approve", "r": "Request changes"}.get(expected, expected)
            ok, outcome = False, f"⚠️ verdict changed to '{verdict or '?'}' (you confirmed {shown}) — not posted, re-check"
        elif expected == "a":
            ok, outcome = run_publish(["--ack", pr, pr])
        elif expected == "r":
            ok, outcome = run_publish([pr])  # request-changes posts without the approve guard
        else:
            ok, outcome = False, "unknown verdict code"
    elif action == "comment":
        ok, outcome = run_publish(["--as-comment", pr])
    elif action == "skip":
        record_skip(pr)
        ok, outcome = True, "🛑 Skipped (no post)"
    else:
        ok, outcome = False, "unknown action"

    api("answerCallbackQuery", callback_query_id=cq_id, text=outcome[:200])
    # Edit the original message: append outcome, drop the buttons.
    orig = msg.get("text", f"PR #{pr}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    api(
        "editMessageText",
        chat_id=chat_id,
        message_id=msg.get("message_id"),
        text=f"{orig}\n\n— decision: {outcome} ({stamp})",
        reply_markup=json.dumps({"inline_keyboard": []}),
        disable_web_page_preview="true",
    )
    print(f"[bot] #{pr} {action} -> {outcome}", file=sys.stderr)


def main():
    print(f"[bot] listening (chat={CHAT_ID}); repo={REPO_ROOT}", file=sys.stderr)
    offset = read_offset()
    while True:
        resp = api("getUpdates", offset=offset, timeout=25, allowed_updates=json.dumps(["callback_query"]))
        if not resp or not resp.get("ok"):
            time.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            write_offset(offset)
            if "callback_query" in upd:
                try:
                    handle_callback(upd["callback_query"])
                except Exception as e:  # noqa: BLE001
                    print(f"[bot] handler error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
