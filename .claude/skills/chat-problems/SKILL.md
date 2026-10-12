---
name: chat-problems
description: Analyse the last N days (default 7) of production chats and report the concrete problems users are hitting, ranked, with proposed beads. Builds on the nightly analyze-conversations results in chat_history.db. Use when asked what problems users are having, to review recent production chats, or for /chat-problems [days].
---

# Production chat problems

Argument: number of days, default 7. Production only.

The nightly `analyze-conversations` CronJob (02:30 UTC) has already judged every
conversation: disposition, a 1–5 quality score, free-text `issues` and a mapping of those
onto a fixed taxonomy (`conversation_prompts.py` in genetics-mcp-server). That taxonomy is
too coarse to act on — "missed_data_source" is not a fix. **This skill's job is the step
the nightly run does not do: group the free-text issues and user signals into concrete,
fixable problems, verify the big ones against transcripts, and propose work.**

Everything here is read-only against production. Never write to the DB, never restart or
re-run the CronJob without asking.

## 1. Extract

```bash
PROD=gke_daly-finngenie_us-central1-a_finngenie
SKILL=.claude/skills/chat-problems
kubectl --context $PROD -n genetics exec -i deploy/chat-backend -- \
  python3 - <DAYS> < $SKILL/extract.py > <scratchpad>/chat-problems.json
```

One JSON document: `last_analysis_run`, `distinct_users`, and `sessions[]` (every session
updated in the window) with `disposition`, `quality`, `issues`, `issue_categories`,
`user_rating`/`user_comment`, `thumbs_down`, `abnormal_turns` (`error`/`cancelled`
counts from `chat_turn_metrics`, inside the window), `first_user_message` and
`unanalyzed`. Users are per-run pseudonyms (`u1a2b3c`); they are not stable across runs.

It is about 60 KB per week. For windows over ~14 days summarise with `python3 -I` first
rather than reading it whole, but always read every `issues` string in full — they are
the material.

## 2. Check the input is fresh

- `last_analysis_run` older than ~26 h means last night's job failed:
  `kubectl --context $PROD -n genetics get jobs -l app=analyze-conversations` and its
  logs. Report it at the top; the rest of the report is then partly blind.
- `unanalyzed` sessions (new since 02:30, or continued after it) have no judge verdict.
  Do not drop them silently: count them, and treat their `abnormal_turns` and user
  signals as evidence like any other.

## 3. Find the problem sessions

A session is a candidate when any of these hold: non-empty `issues`; `disposition` not
`good_answer`; `quality` ≤ 3; any `abnormal_turns`; `user_rating` ≤ 2 or a
`user_comment`; `thumbs_down` > 0. User-side signals (errors, cancels, ratings, thumbs,
comments) outrank the judge's opinion — they are what the user actually experienced.

## 4. Cluster into concrete problems

Group by **root cause**, not by taxonomy category. A good cluster name says what breaks
and where: "turns error out with no reply on large multi-locus requests", "assistant
queries gene_burden_results_v by gene only and hits the scan cap", "no capability to
annotate an uploaded XLSX". One session can feed several clusters. Drop issues that are
cosmetic one-offs (a leftover "wait, no" in a toy example) unless they recur.

For each cluster record: sessions, distinct users, severity (**blocking** — the user did
not get what they came for; **degraded** — answer wrong or misleading; **friction** —
got there with waste), and the layer it most likely lives in: platform/infra, a tool or
its data, the system prompt/instructions, model behaviour, or a genuine capability gap.

## 5. Verify the top clusters

Rank by severity × distinct users. For the top ~4 (fewer if the week is quiet), launch
one `general-purpose` subagent per cluster **in a single message** so they run in
parallel. Give each the cluster, its session ids, and:

- the transcript command (writes to the scratchpad; read it from there):
  ```bash
  kubectl --context gke_daly-finngenie_us-central1-a_finngenie -n genetics exec -i \
    deploy/chat-backend -- python3 - <SESSION_ID> < .claude/skills/chat-problems/transcript.py
  ```
  `[tool_result FAILED: …]` marks tool errors; `===== USER …` lines repeated with no
  assistant turn between them are retries that got no answer.
- for `error` turns, Cloud Logging has what the DB does not:
  `gcloud logging read --project daly-finngenie` filtered on
  `resource.labels.container_name="chat-backend"` and `jsonPayload.message:"<word>"`
  (not `textPayload` — it silently returns nothing) within the turn's time window.
  The sandbox, db-api and results-api containers log their side of tool failures.
- the sibling code is at `~/suite/genetics-mcp-server` (tools, prompts, chat backend),
  `~/suite/genetics-results-api`, `~/suite/genetics-results-db`.
- the ask: confirm or refute the cluster's root cause with quoted evidence (session id +
  turn timestamp), point at the code or config responsible if found, and say plainly
  when the evidence does not settle it. Return a short report, not the transcripts.

Do not drill into clusters below the cut; list them.

## 6. Propose beads — do not create them

For each verified problem, decide the outcome with the filing gate in the global
CLAUDE.md, and check for an existing bead first (`bd search "<keywords>"` and
`bd search --desc-contains "<keyword>" --status all`, in **this repo and in
`~/suite/genetics-mcp-server`** — most chat fixes land there, so the bead belongs in the
repo where the fix is). Outcomes:

- **file** — a defect or gap worth fixing now; give the repo, title, type, priority and a
  description draft
- **ice** — real but nothing committed depends on it (`--labels=icebox`, then `bd defer`)
- **append** — an existing bead covers it; give its id and the note to add (`bd note`)
- **record only** — one-off or not actionable

Bead text is committed to git: describe the problem in your own words with session ids
as evidence. No user pseudonyms, no quoted user prompts, no attachment or data content.

Present the proposals and **stop**. Create nothing until the user picks which to file.

## 7. Report (terminal)

```
## Production chat problems — last N days (<from> → <to> UTC)
Coverage: S sessions, U users; A analysed, X unanalysed; last nightly run <ts> [OK|STALE]
Signals: E error turns, C cancelled, T thumbs-down, R low ratings/comments

### 1. <cluster name> — <severity>, <n> sessions / <u> users   [verified|unverified]
What happens: …
Cause: … (`path/file.py:line` if found)
Evidence: <session id> <ts> — …
Proposal: file in <repo>: "<title>" (P<n>) | append to <id> | ice | record only

…

### Smaller / unverified
- <cluster> — n sessions — one line each

### Nightly analysis gaps
<only if any> problems visible from user signals or transcripts that the judge's
`issues` missed or miscategorised — candidates for improving analyze_conversations
```

End with the bead proposals as a numbered list the user can answer by number.
