# Architecture alternatives: critical evaluation of literature evidence

Output of the architecture-explorer agent, 2026-09-27, for epic `genetics-results-suite-p1e0`.
Input: `00-README.md`, two of the review reports, and the mcp-server code surfaces named there.
Nothing here is decided; the user picks.

## What the code adds to the README's picture

- A tool result reaches the model as one JSON string capped at `settings.mcp_max_result_size`
  (50,000 chars, `llm_service.py`). A Europe PMC call at `max_results=25` with 1,500-char
  abstracts is already near that cap, so any field added to a literature record has a hard
  ceiling.
- The `[n]` mismatch is one line: `_format_perplexity_literature_results` does
  `for entry in entries[:max_results]` while `summary` indexes the full `search_results` list.
  `total_found` says how many there were; nothing says which ones the model cannot see.
- The literature subagent's raw records never reach the main model: `run_subagents` returns
  prose, `tools_used` and token counts (`subagent.py`). Anything the main loop does with
  records is blind to literature fetched by a `literature_review` subagent.
- The replay harness deliberately drops tool results (`extract_tool_calls` keeps calls only,
  `replay_benchmark.py`). Scoring a replay against records needs a bounded exception for
  `search_scientific_literature` results.
- Precedent for a synthetic in-turn correction: `CONTINUE_UNFILLED_PROMPT` (`defaults.py`) is
  sent as a user turn when the model ends with placeholders, bounded by `max_continuations`
  (default 3).
- The prompt's own `## Prohibited` list says "Burying caveats at the end". A design that
  appends a check footer contradicts a rule the model is told to obey.
- Heading pins: `_NOCODE_HEADINGS` / `_DOMAIN_SECTIONS` in `tests/test_system_prompt.py` pin
  every emitted H2/H3 per profile. Adding a heading is a deliberate test edit; adding bullets
  to an existing block is not. A clause naming `search_scientific_literature` is gated by the
  implicit name gate.
- Label split in the two reports read: mjdaly-3 has 16 findings in record-decidable
  categories (1/2/5/8: design, tier, small-n, review) against 12 claim-only (3/4/7: not in
  record, summary-as-source, provenance); finucane-1 has 8 against 17. Roughly half of what
  the humans found cannot be decided from any per-paper metadata.
- The nightly judge is per-session, `content` only, `max_tokens=1000`, and a taxonomy change
  re-categorises every stored issue, so wiring a new category into it has side effects.

## The measurement (common to all three; runs first, per the kill criterion)

- `scripts/conversation_prompts.py`: `LITERATURE_EVIDENCE_JUDGE_PROMPT`, per **turn**, reads the
  question, the answer and the `search_scientific_literature` results matched by
  `tool_use_id` from `content_json` + `tool_results_json`; returns findings in the rubric's
  eight categories with a verbatim claim and the record checked, plus overcautious /
  boilerplate-caveat flags and counter-examples. Absolute and record-grounded; the pairwise
  judge stays for overall quality and the overcautious guard.
- `scripts/literature_judge.py` (new): one definition of "literature-bearing turn", a loader
  for the `[F..]`/`[G..]` entries in `review-*.md`, agreement scoring (per turn-category
  presence; precision on the counter-examples), `--db` baseline over prod rows, `--report`
  over a replay JSON.
- `scripts/replay_benchmark.py`: `TurnRecord.literature_results` from the `done` chunk, the
  one stated exception to the no-results rule; a literature-bearing case selection by
  `tools_used`.
- Calibration hazard: several labels were made on the review dump, which truncated
  literature results at 9,000 chars, and April–June labels have no persisted results.
  Agreement must be computed on labelled turns whose full record is available, after
  re-checking the label against the full record.
- Cost of the baseline: one Opus-class call per literature-bearing prod turn since July,
  nothing deployed.

## Alternative 1: Told. A literature-evidence rubric the model applies itself, per paper, in Pass 2

Give the literature side the same kind of rubric the genetics side has, as a *format* rule:
every citation row in Pass 2 carries design / model system / n / replication /
retrieved-vs-recalled; functional and mechanistic papers get named tiers (cell line, single
mouse line, case report, MR, review, preprint); a review is a pointer to primary studies, not
evidence; the Perplexity `summary` is not a source; a caveat stated in Pass 2 must survive
into Pass 3; converging evidence is weighed, not counted. The subagent skill gets the same
columns and loses "Do not editorialize". Unit of grading: the paper (row), with Pass 3's
existing "every claim must reference specific items" carrying it to the claim.

Files: `config/prompt_condensed.py` (one ungated science block, one gated clause about `[n]`
markers and id-less snippets), `skills/instructions/literature_review.md`,
`tests/test_system_prompt.py` pins, suite `docs/chat-tool-reference.md` 4a and
`docs/project-spec.md`.

Pros: cheapest to build and reverse; reaches every surface; the only alternative that can
address the largest class (provenance blending), because only the model knows what it
recalled; the kill criterion already sequences it first.

Cons: ~350–500 tokens on every turn (cached prefix; +5% of the no-code prompt, <1% of the
code prompt); nothing is verified, a "Retrieved" column is the model's claim about itself;
instrument sensitivity (the 2026-09-12 prompt A/B was 9 wins to 8 with 35 ties); highest
overcautious risk (boilerplate caveats that de-rate nothing are a labelled failure shape).
Cannot fix `[n]` markers pointing at invisible records, abstracts that do not state n, or a
subagent report relayed by a model that never saw the records.

Complexity: low; the measurement work dominates.

## Alternative 2: Shown. The tool result carries per-record appraisal metadata the server already has

Make the record honest before the model reads it. Europe PMC's `core` response carries
`pubTypeList`, `citedByCount`, `meshHeadingList`, `publicationStatus` and `source` (PPR =
preprint); `_format_literature_results` drops all but `is_preprint`, so hydration cannot copy
them onto Perplexity hits either. Keep them in a per-record `evidence` block with a
`record_kind`: `europepmc` (hydrated, abstract present), `perplexity_snippet` (URL + snippet,
no id), `cited_only` (referenced by the summary but beyond `max_results`). Map every `[n]` in
`summary` to a title+url, and label `summary` as AI-generated prose whose claims belong to
Perplexity, not to any paper. Guidance rides in the tool description plus one bullet in the
gated prompt block. Fields are deterministic, never a model's opinion. (Live check on
2026-09-27: all five fields are present in a `resultType=core` response.)

Files: `tools/orchestration.py` (three formatters), `tools/definitions.py` description,
`config/prompt_condensed.py` one bullet, `literature_review.md`,
`tests/test_literature_search.py`, mcp-server `docs/project-spec.md`, suite
`docs/chat-tool-reference.md` section 8.

Pros: deterministic and additive; persists into `tool_results_json` so the judge sees it;
fixes two verified mechanical defects no prompt can (markers indexing invisible records;
Perplexity hits with an id losing metadata Europe PMC already returned); `is_preprint` is the
one field the model already carries through consistently; cost confined to literature calls
(+1–2 KB per call).

Cons: addresses only the record-decidable half of the labels; cannot say whether a claim is
in the record or mark a recalled claim; a third of Perplexity hits get only
`record_kind: perplexity_snippet`; MeSH and pubType lag months and never exist for
preprints; `cited_by` rewards old reviews; size hazard against the 50 KB cap; the subagent
path still relays prose.

Complexity: medium.

## Alternative 3: Checked. A per-claim verification pass over the drafted answer against the retrieved records, inside the turn

After `end_turn` on a turn whose results include a literature result, a second, cheaper model
call reads the final answer beside those records and returns per-claim JSON: claim, cited
record, supported / partial / unsupported / not retrieved, which of design / n / system /
replication the answer omitted although the record states it, whether a Pass 2 caveat was
dropped. Two ways to act: **annotate** (append a "Literature check" block like the budget
notices) or **revise** (feed findings back as a synthetic user turn shaped like
`CONTINUE_UNFILLED_PROMPT`, bounded by `max_continuations`). Unit: the claim; the only
alternative where something other than the answering model reads claim beside record.

Files: new `literature_check.py`; `llm_service.py` hook and cost accounting; `defaults.py`
prompt; `settings.py` flags and the suite's `k8s/deployments/chat-backend.yaml` and
`scripts/deploy.sh`; `subagent.py` to surface literature results; optional
`chat_turn_metrics` column; `tests/test_llm_service.py`, `tests/test_turns.py`.

Pros: the only alternative that can enforce the two claim-level patterns that dominate the
labels (claim welded to a record that does not contain it; caveat dying between Pass 2 and
Pass 3); recalled claims flagged at the moment they are made; unaffected turns pay nothing.

Cons: +10–25K input tokens and 5–20 s per literature-bearing turn on top of $2.01/turn
average; **circular with the measurement** (checker and judge are the same task, so the judge
must be frozen first and a fresh human spot-check of C-arm turns is needed); annotate
contradicts "Burying caveats at the end"; revise appends a correction under text already
read; the checker's own errors are the overcautious rise the kill criterion forbids; sees
nothing a subagent fetched; hardest to reverse (loop call, flag, manifests, a format users
learn to expect).

Complexity: high.

## The decisions the alternatives disagree about

**1. Who appraises: the answering model (told), the server from record metadata (shown), or a
second model reading claim beside record (checked)?** Decides whether the work is prompt
text, formatter code, or a loop change. Settled by: the baseline's category mix over prod
rows, then the replay of alternative 1, which the kill criterion already schedules first. If
categories 3/4/7 dominate and alternative 1 does not move them, telling is exhausted and the
choice is between 2 (cannot address them) and 3 (can, at the cost above). Reversal: 1 is a
commit; 2 leaves harmless extra fields; 3 leaves a call in the loop, a flag in the manifests
and a format users have seen.

**2. What carries the grade: the record or the claim?** Per-record fields are deterministic
and true, but half of the human findings are not decidable from any field. Per-claim grading
reaches them but only via a model. Settled by: counting the 128 labels by record-decidable
(1/2/5/8) vs claim-only (3/4/7) across all eight reports. Reversal: the judge's output schema
and the label mapping change with the unit, so fix it before the judge is calibrated.

**3. Where the appraisal appears: inline in the model's own table and conclusion (1, 2), as an
appended check (3-annotate), or as an appended correction (3-revise)?** The labelled
"caveats die before the bottom line" pattern and the prompt's own prohibition on burying
caveats both argue that an appended block is structurally the failure being fixed. Settled
by: whether the baseline shows caveat-drop as a large share. Reversal: removing a footer is
cheap; a revision loop's interaction with `max_continuations` and cost caps is not.

Not a decision between these three: the backend. All keep Perplexity as the user default;
the reviews found the failure is the paper type, not the tool, and alternative 2 is what
makes Perplexity hits carry the same metadata as Europe PMC records wherever an id exists.
Out of scope for all three but worth its own small item: a declined model's streamed draft is
not persisted, so the complaint's own sentence could not be audited.

## Explorer's recommendation

Run the measurement first regardless (it is the kill gate), then alternative 1, because the
kill criterion already makes it the mandatory first arm and it is the only one that touches
the largest label class at negligible cost. Pair it with alternative 2's deterministic parts
(the `[n]` map, `record_kind`, the Europe PMC fields) if the baseline shows categories
1/2/5/8 are a large share; those are true data the tool currently throws away and do not
depend on the prompt landing. Hold alternative 3 in reserve for the case where claim-level
failures dominate and alternative 1 leaves them standing; its circularity with the judge
means it needs a second human-labelling round to be believed.
