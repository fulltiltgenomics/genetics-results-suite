# Local benchmark inputs (genetics-results-suite-4h6.23)

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
2. **The turn mix is asserted, not observed.** Production was 36% single-iteration turns,
   with 13.5% of turns above 5 iterations consuming 35% of spend. Cases 01–07 here are
   written to be single-iteration and 14–20 to be long-tail, which *approximates* that
   shape. If the run's own arm-A iteration distribution comes out far from it, the set is
   mis-weighted and the cost comparison is measuring a workload nobody has.

## Deliberate exclusions

Both are required by 4h6.23: the code-execution arm cannot win them, for reasons that are
design choices rather than defects, and letting them depress its score silently is the
measurement artefact 4h6.25 was filed to prevent.

- **Clinical variant annotation** (ClinVar / CADD / dbNSFP / pathogenicity, i.e. anything
  behind `get_myvariant_annotations`). The sandbox NetworkPolicy is deny-by-default and
  permits only db-api and results-api, so third-party egress is blocked by construction.
  This is a genuine *unavailability*.
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
> **At or under 10%** — acceptable; the failure rate does not block defaulting the code arm
> on, and the decision falls to the cost and quality gates.
> **Above 10%** — do not default it on whatever the cost line says. The fix is better stubs
> and error messages, not a rollout (carried over from genetics-results-suite-4h6.22).

**Boundary rule, also pre-registered.** This set yields roughly 50–80 script attempts, so a
rate near 10% carries a few points of sampling noise. Judge on the point estimate, but if the
95% interval straddles 10%, the result is **inconclusive** — say so and widen the sample.
Do not round toward whichever answer the cost line makes convenient. That temptation is the
entire reason this number is written down before the run rather than after it.

An arm that never calls `run_analysis` reports `None` (not measured), not `0` — so a zero in
the report is a real zero.

## The arms: `nocode` vs `code`, NOT `all` vs `code`

**Arm A is `nocode`, not the harness's default `all`.** `--arm-a all` sends
`tool_profile: null`, and that profile **contains `run_analysis`** — measured 2026-08-19,
`all` = 65 local tools under the deployed flags, `run_analysis` among them, and `api` and
`bigquery` carry it too. Only `rag` (18 tools) excludes it, and rag is far too narrow to
stand for the old surface.

Left at `all`, the A/B compares *"65 tools including code execution"* against *"7 tools,
code only"* — both arms able to run scripts. That is not old vs new, and it fails in a
direction that is easy to miss: arm A picks up the same context-growth saving the code arm
exists to test whenever the model reaches for `run_analysis`, so the measured gap
**understates** the code arm while the baseline stops being the pre-epic system at all.

`nocode` (`{general, api, bigquery}`, 62 tools deployed) is `all` minus exactly
`{run_analysis, list_capabilities, read_artifact}`. Since genetics-results-suite-4h6.69 the
system prompt is assembled from the tool list in force, so this arm also loses every mention
of `run_analysis` from its prompt automatically — verified, the word does not appear.

**A typo in `--arm-a` is silent and costly.** `get_anthropic_tools` resolves an unrecognised
profile to `{"general"}` — 18 tools — and nothing raises. `--arm-a all` is safe only because
the harness maps the literal `"all"` to `None` before sending. Any other misspelling produces
a crippled baseline that runs fine and reports plausible numbers. Check the arm's tool count
in the report before trusting a run.

## Running it

The stack must be up first, and both arms must see `SANDBOX_ENABLED=true` with the sandbox
actually reachable. A baseline arm replayed against an unreachable sandbox is being steered
toward a path that fails at the transport, which depresses arm A and inflates arm B's win.
That hazard is much smaller with `nocode` than it was with `all` — an arm with no
`run_analysis` in its tool list has no code path to be steered toward — but arm B still needs
the sandbox up.

```bash
# 1. bring everything up (chat-api :4000, results-api :2000, db-api :8080)
scripts/dev-stack.sh up
scripts/run-sandbox-local.sh          # publishes the sandbox on :8081
curl -s localhost:8081/health          # must be {"status": "ok", ...}

# 2. prove the whole chain, not just the health endpoints
scripts/test-e2e-local.py

# 3. resolve the plan without issuing a single request
cd ~/suite/genetics-mcp-server/.claude/worktrees/db-only-architecture
.venv/bin/python -m genetics_mcp_server.scripts.replay_benchmark \
  --dataset ~/suite/genetics-results-suite/.claude/worktrees/db-only-architecture/benchmarks/eval_dataset_local.json \
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

Production averaged $2.01/turn. This set is 20 cases × 2–3 turns ≈ 54 turns, run on **both**
arms ≈ 108 turns. Budget accordingly, and use `--limit` first. `--judge` prices itself before
the first call.
