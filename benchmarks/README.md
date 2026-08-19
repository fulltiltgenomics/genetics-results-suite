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

> **Threshold: NOT YET SET.** Write it into genetics-results-suite-4h6.23 before running.

An arm that never calls `run_analysis` reports `None` (not measured), not `0` — so a zero in
the report is a real zero.

## Running it

The stack must be up first, and both arms must see `SANDBOX_ENABLED=true` with the sandbox
actually reachable. A baseline arm replayed against an unreachable sandbox is being steered
toward a path that fails at the transport, which depresses arm A and inflates arm B's win.

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
  --arm-a all --arm-b code \
  --model claude-opus-5 --provider anthropic \
  --dry-run

# 4. smoke it on three cases before committing to the full run
#    (drop --dry-run, add --limit 3 --output /tmp/smoke.json)

# 5. the full run, then judge separately from the saved report
#    (drop --limit; --judge is Opus-5 spend ON TOP, doubled by both presentation orders)
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
