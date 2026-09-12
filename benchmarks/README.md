# Local benchmark inputs (genetics-results-suite-4h6.23)

> **4h6.23 was descoped on 2026-08-30 without a completed paired A/B run.** Initial
> benchmarking was done by hand and further benchmarking moves outside that epic. The rollout
> question this set existed to answer is **settled by the bead's own kill criterion** — *"if
> the code arm does not beat the baseline on cost AND does not regress quality, keep it behind
> the profile rather than defaulting it on"* — whose conservative branch is the status quo:
> **code execution stays opt-in — it is not turned on for everyone by default.** Which
> profile a deployment starts its users on is its own setting, `DEFAULT_TOOL_PROFILE`
> (`scripts/deploy.sh`, `k8s/deployments/chat-backend.yaml`), and staging runs it on `code`
> (`docs/environments.md`); `null` is a wire value that coerces to the no-code surface, not
> a suite-wide default. The arms were
> never compared, so read no result into that; the decision was not taken on numbers. There is
> no 4h6.23 report and **no paired A/B result exists — no figure below is one.** Individual
> figures below *were* measured, some of them at real cost; read each for what it says it is.
>
> Everything below stands as the design and pre-registration for whoever does the manual
> benchmarking, which is why it is kept rather than deleted.
>
> **The code arm is not the surface any figure here was measured against.** The two-profile
> collapse re-pointed it: it is now the sandbox's own tools plus everything the in-sandbox
> SDK cannot stand in for, and the proxied external and RAG servers reach it as well. The
> `nocode` baseline is unchanged in kind — it is still every data tool and none of the
> sandbox's — so the design below survives, but **no number on this page describes today's
> code arm**. `genetics-results-suite-cgr9.6` is the re-measure. The harness no longer
> accepts a code arm that resolved without `run_analysis`; see the preflight below.

`eval_dataset_local.json` is the question set for the paired A/B replay benchmark. It is
**hand-authored**, not exported from production, and that difference is the most important
thing on this page.

## What this set is not

`replay_benchmark.py` was built to replay `eval_dataset.json` from
`analyze_conversations --export-eval`, i.e. **real recorded conversations**, sampled across
topics and across the success-score range. That file needs `chat_history.db`, which exists
only on the cluster's `chat-data` PVC. Nothing on a developer machine can produce it.

So this set trades realism for being runnable today. Two consequences that must be carried
into any report produced from it:

1. **The 2026-08-07 production figures are not a comparison baseline.** They never were —
   4h6.23's own preconditions already retired them, because the `.17`/`.69` cycle changed
   the system prompt for every profile. This set retires them a second time, on a second
   axis: these are not the questions those numbers were measured over. The paired run's own
   arm A is the only baseline.
2. **The turn mix is deliberately NOT production's.** Production was 36% single-iteration
   turns, with 13.5% of turns above 5 iterations consuming 35% of spend. The set no longer
   approximates that (see "What the set covers" below) — it is weighted to the long tail on
   purpose, because the easy end had stopped separating anything. So a cost total from this
   set is **not** a per-turn production cost and must never be quoted as one; what it
   compares is two arms over the same hard questions.

## What the set covers

**Trimmed from 23 cases to 9 on 2026-09-12.** The cut was the easy end, and the reason is in
the run that preceded it (a prompt A/B over the same 23 cases, 56 paired turns): **seven of
the fourteen dropped cases tied on every turn** — 01, 02, 03, 06, 07, 12 and 21, 15 turns
and 15 ties — while the nine kept cases carry **9 of the 17 decisive verdicts in 24 of the
56 turns**. A question both arms answer identically in one iteration costs money on every
run and separates nothing; it can only report that two arms agree about `PCSK9`'s
coordinates.

What is left is seven `complex` cases and two `moderate`, chosen to keep breadth while
dropping the short tail. Between them they reach the fine-mapping, colocalization,
gene-burden, caQTL/open-chromatin, peak-to-gene, HLA, variant-annotation and
phenotype/dataset-registry verticals, plus the three surfaces that are not BigQuery at all:
the plotting path (`local-22`), the external drug/literature/UniProt tools (`local-23`), and
the multi-source evidence assembly that has to choose between them (`local-20`).

**Not reached, and not to be read as covered:** the four rare-CNV and dosage-sensitivity
views, ASM-QTL, single-variant exome results (`exome_variant_results_v` — gene-level burden
is covered, per-variant is not), and MPRA except incidentally, when `local-20` reaches for
it. Adding any of them means writing a case with a pre-registered answer, not repurposing
one of these.

## Deliberate exclusions

One of the two has lost its premise (see below); the other still stands. Both were
pre-registered because the code-execution arm cannot win them for reasons that are design
choices rather than defects, and letting them depress its score silently is the measurement
artefact 4h6.25 was filed to prevent.

- **Clinical variant annotation** (ClinVar / CADD / dbNSFP / pathogenicity, i.e. anything
  behind `get_myvariant_annotations`). The sandbox NetworkPolicy is deny-by-default and
  permits only db-api and results-api, so third-party egress is blocked by construction —
  but **the premise of this exclusion is gone**: the code surface now keeps every tool the
  SDK cannot stand in for, `get_myvariant_annotations` among them, precisely because no
  script can reach that data. The tool is on both arms. Whether to reinstate these questions
  is a decision for whoever runs the benchmark, not a fact about availability any more.
- **Phenotype reports and gene-prioritisation scores** (the `Score` column, TIER1/TIER2/
  TIER3/CASCADE flags). results-api *is* on the sandbox egress allow-list and does serve
  the document, so the data is reachable — but neither the SDK nor the shipped stubs name
  the route, so a model would have to invent the HTTP call. This is a *discoverability*
  gap, not an availability one. Book it under that reason if it is ever reinstated.

If either is added back, record it as a known, explained loss reported **separately** from
the arm's score.

## Pre-registration — DO THIS BEFORE THE FIRST PAID RUN

4h6.23 requires the acceptable script-failure rate to be written down *before* spending, not
chosen after seeing the number. The harness measures it directly: `llm_service` emits a
`script_result` chunk per `run_analysis` with five disjoint outcomes, and sandbox faults are
excluded from both sides of the rate (4h6.71).

> **Threshold: 10%. Set 2026-08-19, before any paid run.**
>
> Applies to `script_failure_rate` exactly as the harness defines it:
> `(executed_failed + model_rejected) / (executed_ok + executed_failed + model_rejected)`.
> `infra` (sandbox faults) and `TurnBudgetExceeded` are in neither half.
>
> These gates bind **only if someone reopens the default question** — the shipped default is
> settled (see the note at the top of this page), so nothing below re-decides it.
>
> **At or under 10%** — acceptable; the failure rate does not block defaulting the code arm
> on, and the decision falls to the cost and quality gates.
> **Above 10%** — do not default it on whatever the cost line says. The fix is better stubs
> and error messages, not a rollout (carried over from genetics-results-suite-4h6.22).

**Boundary rule, also pre-registered.** The script-attempt count this set yields is a
property of the code arm and **awaits re-measurement** — the earlier "roughly 50–80" was read
off a run against the old code surface. Take it from the run's own
`executed_ok + executed_failed + model_rejected`. At any plausible size a rate near 10%
carries a few points of sampling noise: judge on the point estimate, but if the 95% interval
straddles 10%, the result is **inconclusive** — say so and widen the sample. Do not round
toward whichever answer the cost line makes convenient. That temptation is the entire reason
this number is written down before the run rather than after it.

An arm that never calls `run_analysis` reports `None` (not measured), not `0` — so a zero in
the report is a real zero.

## The arms: `nocode` vs `code`

**They are the harness's defaults now** — `--arm-a nocode --arm-b code`. The surface is one
boolean: `code` selects code execution and *every* other value, `null` included, selects the
no-code surface, so `--arm-a all` (which sends `tool_profile: null`) is a second spelling of
arm A rather than a broader arm, and the preflight refuses the pair.

That closes the trap this section was written about. Before the collapse, `null`, `api` and
`bigquery` all carried `run_analysis`, so leaving arm A at `all` compared two arms that could
both run scripts: arm A picked up the same context-growth saving the code arm exists to test,
which **understates** the code arm while the baseline stops being the pre-epic system at all.

Measured 2026-09-06 by resolving both surfaces under the deployed chat-backend flags
(`SANDBOX_ENABLED=true`, `ENABLE_SUBAGENTS=false`, everything else at its `settings.py`
default, which disables `get_credible_sets_stats`, `get_phenotype_report` and
`launch_subagents`):

| arm | local tools | what it is |
|---|---|---|
| `nocode` (= `null`, `api`, `bigquery`, `rag`, anything unrecognised) | 64 | every data tool, none of the sandbox's |
| `code` | 18 | 3 code-execution tools + the 15 the in-sandbox SDK cannot stand in for |

`code` is no longer a subset of `nocode`: 15 of its 18 are also on `nocode` (the externals
the sandbox egress allow-list puts out of a script's reach, plus the entity lookups a model
uses *before* it writes a script), and the 3 it adds are `run_analysis`, `list_capabilities`
and `read_artifact`. Both arms also get the **same** proxied external and RAG tools, which no
longer depend on the profile.

Since genetics-results-suite-4h6.69 the system prompt is assembled from the tool list in
force, so the `nocode` arm loses every mention of `run_analysis` from its prompt
automatically.

### Knowing for sure what each arm was given

Ask the running server. `/chat/v1/tools/resolved` resolves through
`service.resolve_local_tool_names` — the same call the system prompt is assembled from — so
what it reports is what the model was handed:

```bash
curl -s 'http://localhost:4000/chat/v1/tools/resolved?tool_profile=nocode' | jq '.count, .known_profile'
curl -s 'http://localhost:4000/chat/v1/tools/resolved?tool_profile=code'   | jq '.count, .known_profile'
curl -s 'http://localhost:4000/chat/v1/tools/resolved'                     | jq '.count'  # tool_profile: null
```

A running server is the only thing that can answer this, because the flag subtraction happens
in its process: the table above is what the code resolves to under the deployed flags, and a
stack started with different ones resolves to something else.

**Do not use `/chat/v1/tools` for this** — it returns `TOOL_DEFINITIONS` raw, with no profile
filter, no feature flags, and neither the BigQuery nor the subagent list. It cannot answer
what an arm ran with.

**Three ways an arm can be wrong, all of them silent, all now refused** before anything is
spent — on the `--dry-run` path too, with exit 2:

1. **A typo.** An unrecognised profile resolves to the no-code surface and nothing raises,
   deliberately, because the value comes back from rows written by older clients — so
   `--arm-b cod` would have measured a second baseline and reported plausible numbers
   against it. The server flags it as `known_profile: false` and the harness stops.
2. **An arm that is not the surface it names.** The name being recognised says nothing about
   what came back: `SANDBOX_ENABLED=false` subtracts `run_analysis` *after* the surface
   resolves, so the `code` arm arrives without the one tool it exists to exercise. Measured
   2026-08-27, exactly that run completed, was judged, and had the code arm declared the
   winner on a case where it had burned an iteration force-calling a tool it had not been
   given. The harness now requires `run_analysis` on the `code` arm and its absence on any
   other — which also catches a server that predates the collapse, where the baseline still
   carried it.
3. **Two arms that are one surface.** With every name except `code` resolving alike, a pair
   like `all`/`nocode` compares a surface against itself and reports a difference of zero
   that reads as a real result. Equal resolved name sets are fatal.

The resolved counts and names are recorded in the report under `config.arm_tools`, so a
saved run proves what each arm was given rather than leaving it to be re-derived from a tree
that has since moved. A server too old to have the endpoint warns and continues — the run is
still valid, it just cannot carry the proof.

## Raise the chat service's rate limit BEFORE a full run

`chat_api` rate-limits per user: **`RATE_LIMIT_PER_HOUR` defaults to 20,
`RATE_LIMIT_PER_DAY` to 40 and `RATE_LIMIT_PER_WEEK` to 100**. A full run is 48 model turns
per user, so it exceeds all three.

This does not fail cleanly, which is why it needs saying. Measured 2026-08-19: a full run
against the defaults produced **8 ok / 16 error / 29 not_attempted per arm** — the turns
replayed before the refusal kept their cost, everything after cascaded, and the saved report
still looked complete (20 cases, both arms, correct `arm_tools`) while carrying **8 of 53**
matched pairs. It reads as a finished benchmark of a handful of questions.

```bash
RATE_LIMIT_PER_HOUR=2000 RATE_LIMIT_PER_DAY=10000 RATE_LIMIT_PER_WEEK=10000 scripts/dev-stack.sh up chat-api
```

`dev-stack.sh` does not set these, so they must be exported into the environment it starts
chat-api from, and chat-api must be **restarted** for a change to take effect. Verify:

```bash
tr '\0' '\n' < /proc/$(pgrep -f genetics_mcp_server.chat_api | head -1)/environ | grep RATE_LIMIT
```

The harness now aborts on the first 429 with exit 2 rather than spending the rest of the
plan rediscovering it, and the scorecard refuses to present a rate-limited report as a
benchmark. Neither recovers the money already spent on the turns that succeeded, so raise
the limit first.

## Running it

The stack must be up first, and `SANDBOX_ENABLED=true` with the sandbox actually reachable.
The baseline arm cannot be steered toward a failing code path — it has no `run_analysis` to
reach for — but arm B needs the sandbox up, and if the flag is off the harness refuses the
run rather than measuring a `code` arm the flag has quietly stripped the tool from.

```bash
# 1. bring everything up (chat-api :4000, results-api :2000, db-api :8080)
scripts/dev-stack.sh up
scripts/run-sandbox-local.sh          # publishes the sandbox on :8081
curl -s localhost:8081/health          # must be {"status": "ok", ...}

# 2. prove the whole chain, not just the health endpoints
scripts/test-e2e-local.py

# 3. resolve the plan without issuing a single request
#    paths are the two repo roots; any checkout of the branch under test works
cd <genetics-mcp-server checkout>
.venv/bin/python -m genetics_mcp_server.scripts.replay_benchmark \
  --dataset <genetics-results-suite checkout>/benchmarks/eval_dataset_local.json \
  --base-url http://localhost:4000 \
  --arm-a nocode --arm-b code \
  --model claude-opus-5 --provider anthropic \
  --dry-run

# 4. smoke it on three cases before committing to the full run
#    (drop --dry-run, add --limit 3 --output /tmp/smoke.json)

# 5. the full run, then judge separately from the saved report
#    (drop --limit; --judge is Opus-5 spend ON TOP, doubled by both presentation orders)
```

### The per-question scorecard

`replay_benchmark` reports distributions, which answer *"which arm is cheaper"* and cannot
answer *"on which questions"*. For the second view:

```bash
.venv/bin/python -m genetics_mcp_server.scripts.benchmark_scorecard /tmp/full.json
.venv/bin/python -m genetics_mcp_server.scripts.benchmark_scorecard /tmp/full.json --csv
```

One row per case, both arms side by side, over four numbers: **wall clock to done, USD,
tool calls, and the judge's verdict**. It re-measures nothing — it reads the saved report,
so it is free to re-run, and it works on an old report.

Two things it deliberately refuses to do:

- **A case whose turns did not all succeed on both arms is marked `*` and left out of the
  TOTAL**, with the reason printed. An arm that aborted spent less time, less money and
  fewer tool calls than one that finished; summing those side by side scores failure as
  efficiency. This is the same reasoning as the harness's own matched analysis.
- **The judge column is a pairwise verdict, not a score.** `pairwise_judge` picks a winner
  or a tie per turn, blind and in both presentation orders — there is no absolute per-arm
  quality number, and turning wins into points would imply a scale it never produced. A
  multi-turn case shows a tally. A `!` means the judge could identify an arm from the answer
  text on some turn, i.e. the blinding did not hold there.

Costs that were interval-priced (no cache split on the stream) show the bracket midpoint
with `~`; an unrecognised model shows `n/p`, never `0.00`.

The judge column needs per-pair verdicts in the report, which `pairwise_judge` persists
under `judging.pairs`. A report judged by an older build says so rather than showing a
silently empty column.

### Checking what the model actually did

Every turn records its full tool-call sequence under `tool_calls_detail` — name, arguments
and order, one entry per `tool_use` block, with `tool_calls` being its length so the count
and the listing cannot disagree. **Arguments are stored verbatim and untruncated, including
`run_analysis`'s entire script.** That is the point: a count says the code arm made one call
where the baseline made six, and cannot say whether the one call asked for the right thing.

```bash
# ordered call sequence per case and arm, arguments elided to fit
.venv/bin/python -m genetics_mcp_server.scripts.benchmark_scorecard /tmp/full.json --tools
.venv/bin/python -m genetics_mcp_server.scripts.benchmark_scorecard /tmp/full.json --tools \
  --case local-14-burden-coloc-crosswalk --arg-width 200

# the whole untruncated argument, e.g. the script the model wrote
jq '.turns[] | select(.case_id=="local-14-burden-coloc-crosswalk")
    | {arm, turn_index, tool_calls_detail}' /tmp/full.json
```

Previews in `--tools` are elided and mark it with `…`; the report always holds the full
value. Tool *results* are deliberately not recorded — a single call can return thousands of
rows, and the question this answers is what the model **asked for**.

#### Reading the two arms against each other

`--tools` lists one arm and then the other. `--transcript` puts them in two columns, aligned
turn by turn, with the timing that explains each turn above its calls — which is what you
want when the scorecard says an arm was slower and you need to know where the time went.

```bash
.venv/bin/python -m genetics_mcp_server.scripts.benchmark_scorecard /tmp/full.json \
  --transcript --case local-18-caqtl-ibd-celltypes --width 200 --arg-lines 6
```

Each turn shows, per arm: wall clock, iteration count, call count, model time vs summed tool
phases, the slowest iteration, script attempts and failures by shape, and **retry loops** —
the extra roundtrips bought by a script that failed. `[iN]` marks the iteration a call
belongs to, so six calls in one parallel roundtrip are distinguishable from six roundtrips of
one call each.

**What is not measured.** Only `run_analysis` reports a per-call duration (the sandbox's own
wall clock, shown as `sandbox 1.4s`). No other tool is timed on the wire: an iteration's
calls are dispatched with `asyncio.gather`, so its tool phase is roughly the slowest call
plus overhead, **not** the sum of its calls. `tool phases` sums those phases across
iterations; a `+` after it means some iteration's phase was never measured.

`[iN]` and the sandbox durations come from the *stream's* ordering, not from the `done`
chunk, which flattens every iteration's blocks into one list with no boundary. A report
replayed before that capture existed shows neither and says so instead of printing a number
nobody measured. Arguments still come from the `done` chunk — `llm_service` rewrites the copy
it streams, so only the id is taken from there.

Note `secret=true` does not redact these. `llm_service` omits tool input from its log line,
not from the `done` chunk, so a replayed question's arguments do land in the report.

### The two arms bill to two different keys

The replayed turns never use a key from your shell. The harness is an HTTP client — it POSTs
to chat-api, and **chat-api** makes the model calls, using the `ANTHROPIC_API_KEY` that
`dev-stack.sh` reads from `MCP_ENV_FILE` (default `~/suite/genetics-mcp-server/.env`,
gitignored, present only in the main checkout so a worktree run never gets its own copy).

**The judge is a separate path.** `pairwise_judge.py` builds `anthropic.AsyncAnthropic()`
with no arguments, which reads `ANTHROPIC_API_KEY` from **the process running the harness**.
There is no `--api-key` flag. With the variable unset it raises at client construction —
after the expensive replay has already happened, if you passed `--judge` inline.

Point the judge at the same key:

```bash
export ANTHROPIC_API_KEY="$(grep -m1 '^ANTHROPIC_API_KEY=' \
  "${MCP_ENV_FILE:-$HOME/suite/genetics-mcp-server/.env}" | cut -d= -f2-)"
```

The value is bare and unquoted in that file, so `cut -d= -f2-` is exact (and `-f2-` rather
than `-f2` so a value containing `=` survives). Extracting the one variable is deliberate —
sourcing `.env` would execute it and export everything else in it too.

Because the two halves read different environments, they can bill to different accounts.
Run the judge from the saved report rather than inline, so a missing key costs a re-judge
rather than a re-replay:

```bash
.venv/bin/python -m genetics_mcp_server.scripts.pairwise_judge --report /tmp/full.json
```

`.venv/bin/python`, not bare `python`: the editable install points at the **main checkout**,
so a bare interpreter in a worktree imports `genetics_mcp_server` from the wrong tree. This
has bitten twice (genetics-results-suite-6o3).

`--model` is not optional in practice — without it USD is reported as *not priced*, which
is not the same as zero, and cost is half the decision.

## Cost

Production averaged $2.01/turn. This set is 9 cases × 2–3 turns = 24 turns, run on **both**
arms = 48 turns; these are the expensive questions, so budget nearer the top of any per-turn
range than the middle. Use `--limit` first. `--judge` prices itself before the first call.

Every case carries a `class` — `retrieval`, `analysis`, `plot`, `catalogue` or `external` —
naming what the case tests, so a result can be read per class rather than as one total: the
routing rule a merged surface would need *is* the split between the classes where a chain of
tool calls wins and the ones where a script does. The harness keeps the field and reads
nothing from it; the per-class split is done on the report.
