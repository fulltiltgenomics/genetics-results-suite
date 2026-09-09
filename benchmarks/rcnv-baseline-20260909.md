# rCNV question set — baseline, no rCNV data loaded (2026-09-09)

`genetics-results-suite-hpa1.1`. The pre-data run of `rcnv-questions.md`, both arms, against
**daly-staging**. Read the question file first; the scoring rules quoted here are the ones
pre-registered there.

## Reachability: what serves what, and how each arm was driven

### The proving ground serves the rehearsal dataset

Measured read-only against `gke_daly-finngenie_us-central1-a_finngenie-staging`:

```sh
kubectl --context=gke_daly-finngenie_us-central1-a_finngenie-staging -n genetics \
  get deploy db-api -o jsonpath='{.spec.template.spec.containers[0].env[*]}'
# PROJECT_ID=daly-finngenie  DATASET_ID=genetics_results_dev  MAX_ROWS=100000
```

So the correction in `docs/bigquery-dev-dataset.md` — *"daly-staging selects its own dataset,
but only from its next deploy"* — has since been applied: staging's db-api reads
`daly-finngenie:genetics_results_dev`, not the daly production `genetics_results`. That is
where `dosage_sensitivity` will be created, and it is reachable from this machine:

```sh
kubectl --context=… -n genetics port-forward svc/db-api 18080:8080
curl -s -H "Authorization: Bearer $INTERNAL_API_SECRET" -H 'Content-Type: application/json' \
  -X POST http://localhost:18080/query -d '{"sql":"SELECT COUNT(*) FROM gene_annotations_v"}'
# 44,996 rows
```

`gene_annotations_v` and `gene_burden_results_v` were queried through exactly this route to
derive the ground truth for S8, S9 and S10.

### Both arms can be driven from this machine — the code arm too

chat-backend runs `REQUIRE_AUTH=true`, `SANDBOX_ENABLED=true`, `DEFAULT_TOOL_PROFILE=code`,
`DEFAULT_MODEL=claude-fable-5-1`. Port-forward it and the surface resolves for both arms:

```sh
kubectl --context=… -n genetics port-forward svc/chat-backend 18000:8000
curl -s -H "Authorization: Bearer $INTERNAL_API_SECRET" \
  'http://localhost:18000/chat/v1/tools/resolved?tool_profile=nocode'   # count 64, known_profile true
curl -s -H "Authorization: Bearer $INTERNAL_API_SECRET" \
  'http://localhost:18000/chat/v1/tools/resolved?tool_profile=code'     # count 20, includes run_analysis
```

**The earlier session's constraint is real but is a property of the headers sent, not of the
port-forward.** `auth/dependencies.py::gateway_asserted_identity` requires *both* the identity
header and `X-Gateway-Auth` carrying `GATEWAY_IDENTITY_SECRET`; the internal secret alone is
`auth_required` case 3, the `mcp-tool` service identity, which `run_analysis` refuses. Measured
here, one probe per route, with the question *"run a tiny script that prints 2+2"*:

| headers sent | `/chat/v1/me` | `run_analysis` outcome |
|---|---|---|
| `Authorization: Bearer $INTERNAL_API_SECRET` | `mcp-tool` | **refused**, `status: SandboxNotConfigured` — *"Code execution requires an authenticated user session and is not available to service callers"* |
| the above **+** `X-Gateway-Auth: $GATEWAY_IDENTITY_SECRET` **+** `X-Goog-Authenticated-User-Email: <allow-listed address>` | that address | **ran**, `status: ok`, `duration_ms: 797`, printed `4` |

So on this cluster, from this machine:

- **nocode arm** — port-forward chat-backend, `Authorization: Bearer $INTERNAL_API_SECRET`,
  `tool_profile: "nocode"`. Caller is `mcp-tool`. Works.
- **code arm** — the same plus the two gateway headers, `tool_profile: "code"`. Caller is the
  asserted address, sandbox dispatch is admitted. Works.

Two consequences worth writing down:

1. **`replay_benchmark` cannot drive the code arm against staging as it stands.** Its HTTP
   client sends only `{"Authorization": f"Bearer {auth_token}"}` — there is no flag for a
   second marker or an identity header — so a staging run through the existing harness gets
   `mcp-tool` and a code arm whose every `run_analysis` returns `SandboxNotConfigured`. That
   is a harness gap, not a cluster one, and it is why the earlier session moved to a local
   dev stack. This run therefore used a purpose-built 60-line SSE client
   (`benchmarks/rcnv_runner.py`) rather than `~/benchmarks/run_benchmark2.sh`; the gate run
   should either reuse it or teach `replay_benchmark` an extra-headers flag.
2. **Sending `X-Gateway-Auth` by hand is impersonating auth-gateway.** It is admissible here
   because the secret came from the operator's own cluster and the address asserted is the
   operator's own, but it defeats the control `genetics-results-suite-4h6.84` added, and it
   should stay a benchmarking measure with a named human behind it. Nothing on the cluster
   was deployed, restarted or modified for this run.

### Rate limits and identities

`chat_api` rate-limits per user at 20/hour, 40/day, 100/week and chat-backend sets none of the
three env vars, so the defaults bind. 17 questions x 2 arms = 34 turns would breach the hourly
limit for one identity, so **the two arms ran under two identities** — the nocode arm as
`mcp-tool`, the code arm as the asserted address — 17 turns each, under the hourly cap. Both
arms sent `secret: true`, so no turn was persisted into staging's `chat_history.db`.

Identity does not enter any question, so this does not confound the measurement; it is
recorded because the gate run has to do the same arithmetic.

### How the run was issued

`DEFAULT_MODEL=claude-fable-5-1` on staging; the request set no `model`, so both arms ran on
the deployed default. One turn per question, no follow-ups, `session_id` unique per
question-and-arm so no question saw another's context. Per turn the client recorded the SSE
`done` chunk's `tool_use` blocks (name and arguments), every `script_result` chunk, and the
`usage` chunk, one output file per arm from `benchmarks/rcnv_runner.py`; both are merged into
the committed `benchmarks/rcnv-baseline-20260909.json`.

## Baseline scores — no rCNV data loaded

**Gate set (the 15): nocode 4/15, code 3/15.** Segment extras (outside the 15): nocode 1/2,
code 0/2. Full transcripts, with every tool call and its arguments, in
`benchmarks/rcnv-baseline-20260909.json`.

S10's ground truth was corrected 45→49 after this baseline ran (a symbol-drift join issue,
see `rcnv-questions.md`). Both arms here refused to answer S10 outright, so neither scored
under the old ground truth or would score under the new one — the correction does not change
the table below.

| # | nocode | code | how it failed (or passed) |
|---|---|---|---|
| S1 NRXN1 HI? | 0 | 0 | both: explicit "not in my data", pointed at Zenodo/gnomAD, refused to quote a number |
| S2 PTEN pTriplo | 0 | 0 | both: explicit refusal, "I won't quote a number from memory" |
| S3 COMT HI? | 0 | 0 | both: explicit refusal |
| S4 CACNA1C scores | **1** | 0 | nocode found DECIPHER's rendering by `web_search` and returned pHaplo 1.00 / pTriplo 1.00, which rounds to the true 0.9990 / 1.0000. code searched too, did not land on that page, and refused |
| S5 which of 3 triplosensitive | **1** | **1** | both answered "none", correctly — but from **ClinGen** dosage curations, not pTriplo. Passes as written; see *Rules that need tightening* |
| S6 how many HI / TS genes | **1** | **1** | both: 2,987 / 1,559 straight out of the abstract via `search_scientific_literature` |
| S7 MECP2 scored? | **1** | **1** | both: "no, the map is autosomal only", with the Zenodo file description quoted. The best answers in the run |
| S8 HI genes in chr22 window | 0 | 0 | nocode listed all 45 protein-coding genes from `gene_annotations_v` (58 tool calls, 190 s, 53 of them one-gene `get_gene_disease_associations`) and never singled out HIRA/SCARF2; code got the 45 and said the database carries no haploinsufficiency metric |
| S9 rank SCZ hits by pTriplo | 0 | 0 | **both got the burden half exactly right** — SCHEMA2, `annotation='PTV'`, the right 10 genes with p-values — and both said the ranking is impossible. Precisely the join the epic exists to enable |
| S10 chr22 TS count | 0 | 0 | both refused; both explicitly said any count would be fabricated |
| G1 NRXN1 DEL OR | 0 | 0 | both: no CNV product, no HPO-keyed phenotypes; both named Collins 2022 as the source |
| G2 FDR DUP genes, HP:0001249 | 0 | 0 | code enumerated `gene_burden_results_v`'s annotation classes to prove no DEL/DUP class exists |
| G3 NRXN1 case/control freq | 0 | 0 | nocode offered ~0.1–0.25 % of cases vs ~0.02 % of controls, both **from unrelated literature** (Ching 2010); the truth is 0.17 % / 0.019 % — the control figure matches to one significant figure, but the case range does not resolve to one, and either way a value from a different cohort is not the loaded value and scores 0. code refused outright |
| W1 top DEL window | 0 | 0 | both: no sliding-window product, no GRCh37 tracks |
| W2 chromosomes with p<1e-8 | 0 | 0 | both: same |
| X1 segments over chr16:29.5–30.2 Mb | 0 | 0 | nocode identified the 16p11.2 BP4–BP5 DEL and DUP segments correctly from ClinGen/ClinVar, but of the 12 HPO ids it produced only HP:0001249 and HP:0001250 are on the real DEL list (and only HP:0100753 on the DUP list) — under 4, so it fails the rule. code named the same ClinGen region and no HPO terms at all |
| X2 genes in 22q11.21 DEL segment | **1** | 0 | nocode answered "≈48 protein-coding genes" by counting `gene_annotations_v` over GRCh38 LCR22-A→D. The true Table S3 count is also 48 — but of *Gencode v19 named* genes, including `AC002472.13`, `POM121L7` and `FAM230A`. Same number, different set: a coincidence, scored correct by the rule as written. code ran the same count over slightly different flanking genes and got **49**, so the coincidence did not repeat |

### What the failures look like

**The failure mode is overwhelmingly honest, not hallucinated.** Across all 34 turns
there is **not one fabricated pHaplo, pTriplo, odds ratio or FDR count**. Both arms open with
some form of "this is not in my data", name Collins et al. 2022 unprompted, and point at
Zenodo record 6347673, gnomAD, DECIPHER or the UCSC `dosageSensitivity` track. Several turns
say outright that a number would be invented — S10 code: *"Any count I produced would be
fabricated, so I'm not going to."*

**The four points that were scored are all reachable without the data**, and that is the most
important thing this baseline establishes:

- S6 and S7 are answerable from the **paper's abstract and the Zenodo file description**, which
  `search_scientific_literature` returns. They are not measuring the suite at all.
- S4 was answered by `web_search` finding **DECIPHER's gene page**, which renders the same
  Collins scores to 2 dp. It scored on one arm and not the other purely on which pages the
  search returned — the same question, the same model, one run apart.
- S5 was answered from **ClinGen dosage curations**, a different resource that happens to agree.

So *"baseline near 0"* holds for everything that requires a per-gene value from the map, and
the residual 3–4 points are the score the external tools carry on their own. **The gate must
be read against 4/15 and 3/15, not against 0** — a carrier that reaches 10/15 has to win the
six or seven questions no external route answered here, and the honest reading is that the
epic's value claim is about exactly those.

**Where the agent went when it could not answer**: `search_scientific_literature` (perplexity)
in 13 of 17 nocode turns and 9 of 17 code turns, `web_search` in 9 and 8. The epic's premise —
*"an agent asked 'is this gene haploinsufficient' cannot answer from the suite and either
guesses or goes to an external tool"* — is confirmed in the second branch, not the first.

**The code arm never gets a script that helps.** `run_analysis` was called on 6 of 15 code
turns, every script ran `ok`, and none of them produced an answer: the sandbox's SDK has no
rCNV surface to call, so the scripts either re-queried `gene_annotations_v` (S8, S10) or tried
to fetch the Zenodo file and **failed on DNS resolution** — the deny-by-default egress policy
in `k8s/network-policies/sandbox-policy.yaml` holding exactly as `docs/code-execution-security.md`
describes. Two turns wrote that fact into the answer. Nothing here is a script-quality problem;
there is nothing to write a script against.

**Cost and time.** 34 turns, 1,031 s of wall clock on the nocode arm and 755 s on the
code arm. The `usage` chunks show the turns are dominated by cached system-prompt input
(~59 k input tokens, ~59 k of it cache reads) against tens to a few hundred output tokens, so
the run is far inside the $10 ceiling — the expensive part of this subtask was the two rounds
lost to restarts and the rate limit, not the model calls.

## Rules that need tightening before the gate run

Three of the pre-registered rules did something the ground truth did not intend. They are
recorded rather than silently rewritten, because the gate has to run the *same* questions:

1. **S5** admits an answer sourced from ClinGen. Both arms passed it without ever touching a
   pTriplo value. If the gate is meant to measure the loaded table, the rule should require the
   three pTriplo values, not just the verdict.
2. **X2's number is reachable by coincidence.** 48 GRCh38 protein-coding genes in LCR22-A→D and
   48 Gencode v19 genes in the Table S3 segment are different sets with the same count. The
   rule should require the segment's coordinates (22:18,820,000–21,540,000, GRCh37) alongside
   the count.
3. **S4 and S6 are answerable from public prose.** They stay in the set — a carrier that cannot
   beat DECIPHER on its own data is worth knowing about — but their contribution to the 10/15
   bar should be read knowing both arms can score them with no data loaded.

None of these was changed in `rcnv-questions.md`: the baseline and the gate must be scored by
the same rules, and changing them now would make the two runs incomparable.

## Operational notes for whoever runs the gate

- **The per-user hourly limit bit.** The code arm's last two questions returned HTTP 429 and
  had to be re-issued ~50 minutes later. 17 questions leaves no headroom under
  `RATE_LIMIT_PER_HOUR=20` once a probe or a retried turn is spent, and staging sets none of
  the three limit variables so the defaults bind and cannot be raised without a deploy. Plan
  one arm per hour per identity, or make the client resumable — the runner used here skips
  questions already recorded without an error, which is what made the recovery cheap.
- **A `nohup`-ed background client is not durable here.** Three separate launches were killed
  and restarted mid-question, re-spending turns on questions that had already been answered.
  What survived was a single long-lived foreground process. Combined with the resume rule this
  cost only duplicate turns, but it is why the code arm's S1–S4 appear more than once in the
  raw log.
- **Nothing was mutated on the cluster.** All kubectl use was `get`, `port-forward` and reading
  two secret values to sign requests; no deploy, restart, scale or config change. Both arms
  sent `secret: true`, so staging's `chat_history.db` carries none of these turns.
