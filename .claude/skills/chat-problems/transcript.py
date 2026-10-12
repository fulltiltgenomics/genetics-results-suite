"""Read-only text dump of one conversation, for drilling into a problem cluster.

Runs INSIDE the chat-backend pod:
    kubectl --context <prod> -n genetics exec -i deploy/chat-backend -- python3 - SESSION_ID < transcript.py

Base64 artifacts are replaced by their names and tool results are truncated, so a long
sandbox session stays readable. Tool calls and results exist only on messages from about
July 2026 on; older turns carry inline [TOOLUSE:...] markers instead, decoded here.
"""
import base64
import json
import re
import sqlite3
import sys

RESULT_MAX = 1500
TEXT_MAX = 6000
MARKER = re.compile(r"\[(IMAGE|FILE):([^:]+):([^:\]]+):[A-Za-z0-9+/=]+\]")
TOOLUSE = re.compile(r"\[TOOLUSE:([A-Za-z0-9+/=]+)\]")


def clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f" …[+{len(text) - limit} chars]"


def decode_tooluse(match):
    try:
        call = json.loads(base64.b64decode(match.group(1)))
        return f"[tool_use {call.get('name')}: {clip(json.dumps(call.get('input')), RESULT_MAX)}]"
    except Exception:
        return "[tool_use: undecodable]"


def result_text(content):
    if isinstance(content, list):
        return " ".join(c.get("text", f"[{c.get('type')}]") for c in content if isinstance(c, dict))
    return str(content)


db = sqlite3.connect("file:/data/chat_history.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
sid = sys.argv[1]
s = db.execute("SELECT title, created_at, rating, comment FROM chat_sessions WHERE id = ?", (sid,)).fetchone()
if s is None:
    sys.exit(f"no session {sid}")
print(f"# {s['title']}  (created {s['created_at']}, rating {s['rating']}, comment {s['comment']!r})\n")

for m in db.execute(
    "SELECT role, created_at, content, content_json, tool_results_json, thumbs_up "
    "FROM chat_messages WHERE session_id = ? ORDER BY created_at, rowid",
    (sid,),
):
    thumbs = {1: " 👍", 0: " 👎"}.get(m["thumbs_up"], "")
    # not a markdown heading: assistant text is full of those
    print(f"===== {m['role'].upper()} {m['created_at']}{thumbs} =====")
    blocks = json.loads(m["content_json"]) if m["content_json"] else None
    if isinstance(blocks, list):
        for b in blocks:
            if b.get("type") == "text":
                print(clip(MARKER.sub(r"[\1 \3]", b.get("text", "")), TEXT_MAX))
            elif b.get("type") == "tool_use":
                print(f"[tool_use {b.get('name')}: {clip(json.dumps(b.get('input')), RESULT_MAX)}]")
    else:
        text = TOOLUSE.sub(decode_tooluse, m["content"] or "")
        print(clip(MARKER.sub(r"[\1 \3]", text), TEXT_MAX))
    for r in json.loads(m["tool_results_json"]) if m["tool_results_json"] else []:
        text = result_text(r.get("content"))
        failed = r.get("is_error") or re.search(r'"success":\s*false', text[:500])
        print(f"[tool_result{' FAILED' if failed else ''}: {clip(text, RESULT_MAX)}]")
    print()
