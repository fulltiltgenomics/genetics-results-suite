"""Read-only extract of the nightly conversation analysis for one time window.

Runs INSIDE the chat-backend pod (stdin to `python3 -`), so it uses only the stdlib:
    kubectl --context <prod> -n genetics exec -i deploy/chat-backend -- python3 - DAYS < extract.py

Prints one JSON document. User ids are replaced by salted per-run pseudonyms, because the
output ends up in a terminal transcript and possibly in bead text that is committed to git.
"""
import hashlib
import json
import os
import sqlite3
import sys

days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
since = f"-{days} days"
db = sqlite3.connect("file:/data/chat_history.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
salt = os.urandom(8)
pseudonyms: dict[str, str] = {}


def pseudonym(user_id):
    if user_id is None:
        return None
    if user_id not in pseudonyms:
        pseudonyms[user_id] = "u" + hashlib.sha256(salt + user_id.encode()).hexdigest()[:6]
    return pseudonyms[user_id]


def by_session(sql):
    return {r[0]: r[1:] for r in db.execute(sql, (since,))}


# turn outcomes and thumbs are counted inside the window only, so a long-running session
# is not charged with failures from before the window
outcomes = {}
for sid, outcome, n in db.execute(
    "SELECT session_id, outcome, COUNT(*) FROM chat_turn_metrics "
    "WHERE created_at > datetime('now', ?) AND outcome != 'complete' GROUP BY 1, 2",
    (since,),
):
    outcomes.setdefault(sid, {})[outcome] = n
thumbs_down = by_session(
    "SELECT session_id, COUNT(*) FROM chat_messages "
    "WHERE created_at > datetime('now', ?) AND thumbs_up = 0 GROUP BY 1"
)

sessions = []
for r in db.execute(
    """
    SELECT s.id, s.user_id, s.created_at, s.updated_at, s.rating, s.comment,
           a.analyzed_at, a.llm_disposition, a.llm_quality_score, a.success_label,
           a.topic, a.metrics_json
    FROM chat_sessions s
    LEFT JOIN conversation_analysis a ON a.session_id = s.id
    WHERE s.updated_at > datetime('now', ?)
    ORDER BY s.updated_at
    """,
    (since,),
):
    m = json.loads(r["metrics_json"] or "{}")
    sessions.append({
        "session_id": r["id"],
        "user": pseudonym(r["user_id"]),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        # missing analysis, or messages added after the last nightly run
        "unanalyzed": r["analyzed_at"] is None or r["updated_at"] > r["analyzed_at"],
        "disposition": r["llm_disposition"],
        "quality": r["llm_quality_score"],
        "success": r["success_label"],
        "topic": r["topic"],
        "messages": m.get("total_messages"),
        "first_user_message": (m.get("first_user_message") or "")[:300],
        "issue_categories": m.get("llm_issue_categories") or [],
        "issues": m.get("llm_issues") or [],
        "user_rating": r["rating"],
        "user_comment": r["comment"],
        "thumbs_down": (thumbs_down.get(r["id"]) or (0,))[0],
        "abnormal_turns": outcomes.get(r["id"], {}),
        "has_error_response": m.get("has_error_response"),
    })

last_run = db.execute("SELECT MAX(analyzed_at) FROM conversation_analysis").fetchone()[0]
print(json.dumps({
    "window_days": days,
    "now_utc": db.execute("SELECT datetime('now')").fetchone()[0],
    "last_analysis_run": last_run,
    "sessions": sessions,
    "distinct_users": len(pseudonyms),
}, ensure_ascii=False))
