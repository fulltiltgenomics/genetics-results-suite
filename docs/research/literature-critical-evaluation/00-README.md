# Is literature evidence critically evaluated in FinnGenie chat?

Review, 2026-09-27, prompted by a user comment: the assistant "does not seem to have the same
kind of guidelines for interpreting work from the literature as it does for the genetics
results we loaded ... it said here's strong evidence of demyelination for schizophrenia that
was based on like 5 people".

All conversations of three users (bneale, mjdaly, finucane) in the production
`chat_history.db` were read in full by eight review agents against one rubric (`rubric.md`).
The per-transcript reports are `review-*.md`; every finding quotes the assistant's claim and,
where the tool result was persisted, the source text it was built from. Numbers below are
dated observations from that day, not maintained facts.

| user | sessions | messages | literature calls (reviewers' count) | findings | counter-examples |
|---|---|---|---|---|---|
| mjdaly | 77 | 422 | 181 | 86 | 69 |
| finucane | 14 | 132 | 45 | 28 | 21 |
| bneale | 17 | 66 | 37 | 14 | 15 |
| total | 108 | 620 | 263 | 128 (8 high) | 105 |

Reading the two columns together: the assistant scrutinises a paper roughly as often as it
does not, and the good behaviour is real. The question is what separates the two.

## Facts verified against the code and the database, not the transcripts

1. **Every literature call in production has gone through Perplexity.** All 2,436 assistant
   turns with a backend recorded say `perplexity`; the Europe PMC backend has never been
   selected by any user. The tool schema exposes no backend parameter, and when the model
   passed `backend: "europepmc"` in its input (it did so in at least 25 calls across these
   three users), `llm_service` overwrote it with the user setting. The model noticed and said
   so in several answers, then used the AI summary anyway.
2. **The Perplexity call uses the `sonar` model with a system prompt that asks for "key
   findings"**, and the `summary` it returns is prose with `[n]` citation markers. The markers
   index Perplexity's own `search_results` list, which the tool cuts at `max_results`
   (default 10), so `[12]`, `[14]` in a summary point at nothing the model can see. Records
   are hydrated from Europe PMC when a PMID/DOI/PMCID can be parsed from the URL; in
   September 2026 about a third of hits still had neither.
3. **Before July 2026 the per-record fields were empty** (title, authors, PMID all blank; only
   a URL list and the summary). Findings from April–June rest on that state and on the
   assistant's text alone, because tool results were not persisted then either. From July
   2026 on the records carry titles and, for most, a PMID or DOI.
4. **The system prompt has a genetics-evidence rubric and no literature-evidence rubric.**
   `config/prompt_condensed.py` carries blocks on pseudo credible sets, PIP interpretation,
   MPRA coverage, the p < 1e-10 flag, AlphaGenome `validation` fields, rCNV significance
   tiers and HLA. The literature guidance is: name the backend, cite as a markdown link, and
   one block about GeneCards summaries that does ask for "sample size, replication, study
   type". The three-pass template says "Present the literature in a structured format. Do
   not draw conclusions yet" and nothing about appraising it. The `literature_review` subagent
   skill ends with "Do not editorialize — report what the literature says".
5. **The nightly quality judge cannot see this failure class.** `conversation_prompts.py`
   tells the judge it is "NOT shown the raw output the data tools returned" and to assume
   figures are real; its issue taxonomy has no literature category (`weak_grounding` and
   `inaccurate_claim` are the nearest). The GPR17 session behind the user's complaint was
   scored 5/5, `good_answer`, no issues.
6. **Europe PMC already returns the fields an appraisal needs and the tool drops them:**
   `pubTypeList` (Review, Case Reports, Meta-Analysis, Randomized Controlled Trial, ...),
   `citedByCount`, `meshHeadingList` (Humans / Animals / Mice / Cell Line), `source` (`PPR` =
   preprint), `publicationStatus`. `_format_literature_results` keeps title, authors,
   journal, year, a 1,500-character abstract, identifiers and `is_preprint`.
7. **Tool results are persisted in full since July** (`content_json` and
   `tool_results_json` on `chat_messages`; median Perplexity summary 3.9 KB). Several review
   reports say results were "truncated" — that was the review dump, which cut non-literature
   results to 400 characters and literature results to 9,000, not the database.

## The complaint's own example

The sentence bneale remembers is not in any persisted message. In the GPR17 session
(2026-09-23) the first literature call returned, among eight hits, Satoh et al. 2017:
"We studied the expression of GPR17 in **five** NHD brains and eight control brains ... we
did not find statistically significant differences". Nasu-Hakola disease is a TREM2/TYROBP
leukoencephalopathy with extensive demyelination and frontal-lobe psychiatric presentation.
That is the only five-person study in the session and the natural raw material for
"demyelination ... schizophrenia ... five people". The persisted answer does not make the
claim; the turn carries the marker "Claude Fable 5.1 declined this request; Claude Opus 5
answered instead", and a declined model's streamed draft is not persisted. So the exact
wording cannot be audited, and that is itself a finding: a complaint about a specific
sentence should be checkable.

## What separates the scrutinised papers from the relayed ones

Eight independent reviewers converged on the same four patterns. Each is illustrated with one
verbatim example; the reports hold the rest.

**1. The rubric transfers to genetics papers and stops there.** Candidate-gene associations
are almost always sized, dated and down-weighted ("76 cases / 74 controls ... very small,
candidate-gene design"; "sample sizes 2–3 orders of magnitude below current GWAS, unreplicated").
Functional, mechanistic and clinical papers get a one-line finding and no model system, group
size, null arm or replication status. In one FURIN turn the loaded burden result gets
"p = 1.46×10⁻⁴ falls ~15-fold short ... CI barely excludes 1", while a HEK293T luciferase
assay, a Drosophila habituation assay and a review are *counted* as "five independent
systems" pointing the same way. In another, a 106-patient glioma study whose abstract
attributes the effect to "intestinal flora" becomes "the replication" that makes a mechanism
"Strong, allele-specific, replicated". A single-cat case report with in-silico support is
"cross-species support for the catalytic domain". This is exactly the shape of the user's
example, which is a non-association claim.

**2. Retrieved and recalled literature are indistinguishable.** Category 7 (provenance) is the
largest class in six of eight reports. Memory-derived sentences are welded to a search citation
that does not contain them (a mouse spinal-cord paper cited for skin wound healing; "46 Olink
and 35 SomaScan pQTLs" attributed to an abstract with no such figure; an OR 2.19 with "CIs
overlap" attributed to a paper whose retrieved text gives neither). Whole "PASS 2 — LITERATURE
CONTEXT" sections are written with no search at all, and two of those carry checkable errors
(MYH7 "most commonly mutated gene in familial HCM"; SNP heritability of substance-use
disorders "~50%", which is the twin figure). One message says "verified via search" in a turn
with no search. The memory content is usually right; when it is wrong nothing in the format
lets a reader tell.

**3. Perplexity's assertions survive transmission, its hedges do not.** "Likely PMCID",
"Authors: not shown", "DOI: not provided", "in the provided search results" are dropped;
"robustly associated", "explicitly identifies", "confirms" are kept and sometimes escalated
("notes" in the table becomes "state directly" in the analysis of the same message). The
summary's reference list is re-emitted as the assistant's citation table with malformed DOIs,
wrong journals and author lists that do not match the returned URLs; in one case a paper
title Perplexity invented was caught by the assistant, in others it was not. Press releases,
patient-foundation sites, EyeWiki and MedlinePlus appear in "Literature" tables with the same
weight as PMIDs.

**4. Caveats live in Pass 2 and die before the bottom line.** "Treat as unverified" in the
literature table becomes "explicitly fails to colocalize" in the conclusion; "preprint" in the
table becomes one of "three independent readouts"; "unverified" in an interim note becomes
"classically known" in the answer. A blanket "the perplexity summaries are AI-generated and
not individually verified" under a verdict table that says "literature favors INO80E" does not
de-rate the verdict. Reconciliation with the loaded data runs one way: a null in the data is
reconciled with the literature carefully; a positive in the data that agrees with a paper is
declared "consistent" without a number on the literature side.

Scrutiny is reactive: the strongest literature appraisals followed a user challenge ("is that
really plausible?") or a result that looked too good. Only one of 620 messages carried a
per-citation **Design** column; it was the only message where the reviewer had nothing to
infer.

## What the existing measurement misses

The nightly judge rates the rendered answer without the tool results, so it cannot see that
"Zhou 2022 reports an OR of 9.37" is SCHEMA's number relayed by a commentary, or that an
abstract says "α1-PDX had no effect" where the answer says the paper is "a direct, quantitative,
causal test". Any fix needs a measurement that reads the persisted `tool_results_json` beside
the answer; the persisted data now make that possible, and the eight reports are a
human-labelled set of 128 findings and 105 counter-examples to calibrate it against.

## Code surfaces a fix would touch

| surface | file (genetics-mcp-server) | what it does today |
|---|---|---|
| chat system prompt | `config/prompt_condensed.py` | genetics rubric blocks; literature: backend name + markdown link + GeneCards block |
| literature subagent skill | `skills/instructions/literature_review.md` | asks for "sample size, method, year" per paper, then "Do not editorialize" |
| tool definition | `tools/definitions.py` (`search_scientific_literature`) | no backend param; describes the two backends |
| backends | `tools/orchestration.py` | `sonar` call, domain filter, `_format_perplexity_literature_results`, `_hydrate_literature_metadata`, `_format_literature_results` (drops pubType, citedBy, MeSH) |
| backend override | `llm_service.py` (`effective_input["backend"] = literature_backend`) | user setting wins over the model's argument |
| nightly judge | `scripts/conversation_prompts.py`, `scripts/analyze_conversations.py` | no tool results, no literature category |
| replay harness | `scripts/replay_benchmark.py` (`--judge`) | paired A/B over recorded conversations, blind pairwise judge |
| prompt tests | `tests/test_system_prompt.py` | heading pins and required strings |
| suite docs | `docs/chat-tool-reference.md`, `docs/project-spec.md` (this repo) | tool surface and prompt gate descriptions |
