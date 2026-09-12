"""Drives the rCNV question set (rcnv-questions.md) against a port-forwarded chat-backend.
Sends the nocode arm as the internal-secret identity and the code arm with the extra gateway
headers needed to pass `run_analysis`; resumable — reruns skip question ids already in the
output file.
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://localhost:18000"
INT = os.environ["INT_SECRET"]; GW = os.environ["GW_SECRET"]
EMAIL = os.environ["BENCH_EMAIL"]

def headers(arm):
    h = {"Authorization": f"Bearer {INT}", "Content-Type": "application/json",
         "Accept": "text/event-stream"}
    if arm == "code":
        h["X-Gateway-Auth"] = GW
        h["X-Goog-Authenticated-User-Email"] = EMAIL
    return h

def ask(arm, qid, text, timeout=600):
    body = {"messages": [{"role": "user", "content": text}],
            "provider": "anthropic", "enable_tools": True,
            "tool_profile": "code" if arm == "code" else "nocode",
            "secret": True, "session_id": f"rcnv-baseline-{arm}-{qid}"}
    req = urllib.request.Request(BASE + "/chat/v1/chat",
                                 data=json.dumps(body).encode(), headers=headers(arm))
    out = {"arm": arm, "id": qid, "question": text, "text": "", "tool_calls": [],
           "script_results": [], "usage": None, "error": None}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line.startswith("data:"):
                    continue
                try:
                    c = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = c.get("type")
                if t == "content":
                    out["text"] += c.get("content", "")
                elif t == "usage":
                    out["usage"] = c
                elif t == "script_result":
                    out["script_results"].append(c)
                elif t == "error":
                    out["error"] = c.get("error")
                elif t == "done":
                    for b in c.get("message_content") or []:
                        if b.get("type") == "tool_use":
                            a = b.get("input") or {}
                            out["tool_calls"].append({"name": b.get("name"), "input": a})
                        elif b.get("type") == "text" and not out["text"]:
                            out["text"] += b.get("text", "")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_s"] = round(time.time() - t0, 1)
    return out

if __name__ == "__main__":
    arm = sys.argv[1]; qfile = sys.argv[2]; outfile = sys.argv[3]
    qs = json.load(open(qfile))
    # resumable: a durable client mattered more here than speed, see rcnv-baseline-20260909.md
    res = json.load(open(outfile)) if os.path.exists(outfile) else []
    done = {r["id"] for r in res if not r.get("error")}
    for q in qs:
        if q["id"] in done:
            continue
        r = ask(arm, q["id"], q["q"])
        tools = [c["name"] for c in r["tool_calls"]]
        print(f'{arm} {q["id"]} {r["elapsed_s"]}s tools={tools} err={r["error"]} '
              f'chars={len(r["text"])}', flush=True)
        res.append(r)
        json.dump(res, open(outfile, "w"), indent=1)
    print("DONE", outfile)
