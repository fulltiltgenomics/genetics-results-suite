# Architecture alternatives for `genetics.mr` (architecture-explorer, 2026-10-09)

Epic genetics-results-suite-q51a, Feature Planning Workflow step 3. Verbatim agent output; the user's choice is recorded in the epic.

# `genetics.mr` — architecture alternatives (epic genetics-results-suite-q51a)

## 0. Facts verified in code that bind every alternative

- **SDK data surface** (`/home/jkarjala/suite/genetics-mcp-server/src/genetics_mcp_server/sdk/client.py`): `credible_sets(qtl_gene=|gene=|phenotype=+leads_only=)`, `summary_stats(phenotypes=, variants=)` (one POST; results-api fans out one existence check, one header and one multi-region byte-range read per `(config, phenotype)` file, `app/services/sumstats_data_access.py:_stream_regions`), `ld(variant[, other], panel="finngen_r12", fields=["r","r2"])` (k variants = k calls, no matrix route), `ld_panels()`, `variant_annotation(variants=[...], source="finngen"|"gnomad")`, `search(rsids=)` (`MAX_RSIDS = 5000`, `app/routers/rsid.py:27`), `phenotypes(codes=)` (n_samples/n_cases/n_controls), `datasets()` (`n_samples`, `pseudo_credible_sets`), `colocalization(credible_set_id=)`, `sql()` (25,000-row cap, 50 GB/query, 200 GB/execution).
- **Caps** (`/home/jkarjala/suite/genetics-results-api/app/core/sandbox_budget.py:165-177`): 1,000 requests and 4 in flight per execution; wall clock 120 s ceiling; 2,560 MiB (`docs/code-execution-security.md` limits block).
- **Outcome rows carry AF**: FinnGen `af_alt/af_alt_cases/af_alt_controls → af/af_cases/af_controls`; IIBDGC also `n_cases`, `n_controls`, `n_eff` per row (`app/config/profiles/daly/summary_stats.py:5-27, 325-348`). Outcomes with sumstats on daly: FinnGen R14 endpoints, kanta, drugs, FinnGen+MVP+UKB and FinnGen+UKB (disease and labs), FinnGen pQTL, FinnGen+UKB-PPP pQTL, IIBDGC IBD/UC/CD, PGC SCZ/BIP, COVID-HGI A2/B1/B2/C2, GP2 PD, FinnGen eQTL.
- **LD**: `finngen_r12`, r² floor 0.01, cap 3 Mb, fields `r, r2`, 520,210 samples (`app/config/profiles/daly/ld.py`); ~0.2 s per call; pair route distinguishes "below floor" from "not in panel". Not rolled anywhere yet, but served by a results-api started from source (the daly profile reads `gs://daly-genetics-results/ld/finngen/r12/` through ADC).
- **Instruments are not independent signals**: `datasets_v.pseudo_credible_sets` flags the clumped datasets (deCODE, IIBDGC, COVID-HGI, FinnGen labs); for deCODE the max-PIP row is not always the clump lead — the lead is the variant named in `cs_id` (`docs/research/mendelian-randomization/01-decode-pqtl-issues.md` §6); FinnGen Olink SuSiE splits one signal into several sets.
- **Registration checklist** for an SDK analysis module, measured from the enrichment branch (`git -C /home/jkarjala/suite/genetics-mcp-server show 0d6015b`): `_LAZY_MODULES` (`sdk/__init__.py`), `_SDK_MODULES`/`_SDK_MODULE_SUMMARIES`/`_sdk_members`/`_SDK_CALL_PREFIXES` (`tools/executor.py:5677-5800`), the `module` enum plus a run_analysis paragraph (`tools/definitions.py:2087-2161`), `SHIPPED_OUTSIDE_CLOSURE` (`tests/test_sdk_import_closure.py:303`), six parametrised tests (`tests/test_code_execution_tools.py`), the skill instruction (`src/genetics_mcp_server/skills/instructions/data_analysis.md`), a project-spec section; suite side `SDK_ALLOWLIST` (`sandbox/prune_venv.py:57`), `SURFACE_MODULES` (`scripts/gen-sandbox-docs.py:68`), the generated `sandbox/stubs/mr.pyi`, `docs/chat-tool-reference.md`, `docs/project-spec.md`.
- **Replication tables** (`/home/jkarjala/mr_fixtures_untrusted/`, read with `/usr/bin/python3 -I` + openpyxl, which the mcp-server venv lacks): Zhang Data 8 keys instruments by **rsID**, GRCh38 position, effect/other allele, EAF, beta, SE, `samplesize.exposure = 33,995` (UKB-PPP discovery subset, not the 42,853 of our `UKB_PPP` credible sets), with **K = 3-4 clumped cis instruments per protein**; Data 9 keys proteins by symbol with `OR(95%CI)` strings and only observational-"Yes" proteins carry MR numbers. Our IIBDGC file is the 2026 release (125,992 IBD cases) — almost certainly a larger release than Zhang's. Docker and `rocker/r-ver:4.4.3` precedent exist (`tests/fixtures/linemodels/Dockerfile.r-reference`).

## 1. The shared skeleton (identical in all three alternatives)

- **Estimator core**, a numpy/polars port held to R: Wald, IVW fixed/random (multiplicative), IVW with a signed correlation matrix (`mr_ivw(correl=)`), Egger (slope, intercept, I²_GX), weighted/simple median (seeded bootstrap), weighted/simple mode, Q/I², leave-one-out, single-SNP, Steiger (r² from beta/se/n; log-OR approximation for binary). This is what measurement (a) tests and it does not touch the network.
- **The kill measurement is architecture-independent and runs before any of the three is built.** A ~150-line check in a proving-ground script using only existing SDK data functions: one `sql()` for UKB_PPP cis leads (QUALIFY lead per `(resource,dataset,trait,cell_type,cs_id)`, ±1 Mb of `gene_annotations_v`, mlog10p ≥ 7.3, MHC out), one `summary_stats(phenotypes=["IBD","CD","UC"], variants=..., resource="ibd_gwas")`, Wald ratio in polars, join to a Data 9 fixture, Pearson r on log OR, tier-1 direction table. Layered so a failure localises: **(b0)** Zhang's Data 8 instruments verbatim (rsID → id via `search(rsids=)`, their beta/SE/EAF) against our IIBDGC → isolates outcome-release and harmonisation; **(b1)** our lead-only Wald → the kill number; **(b2)** our pruned multi-instrument IVW → only after the module exists.
- **Proving ground available today**: the local dev stack (`scripts/dev-stack.sh up --tree main`, `docs/local-dev-vm.md`) with results-api from source on the daly profile (so `finngen_r12` is served) and the SDK imported from the mcp-server venv with `INTERNAL_API_SECRET` — no sandbox, no caps, no rollout. Staging (`finngenie-staging`) is needed only for the in-sandbox envelope (wall clock, request count), after rolling results-api and the sandbox image.
- **Harness**: `scripts/mr-proving-ground.py` in the suite (pattern: `scripts/chat-memory-proving-ground.py`, `scripts/external-inputs-proving-ground.py`, 0/1/2 exit codes, sibling resolution via `scripts/lib`); fixtures derived once from the untrusted xlsx into TSV with the source sha256 recorded, committed under mcp-server `tests/fixtures/mr/` (so estimator tests can use Data 8 as realistic input); R reference in `tests/fixtures/mr/Dockerfile.r-reference` (MendelianRandomization from CRAN, TwoSampleMR from the MRC-IEU r-universe, both pinned by p3m date).

---

### Alternative A: In-sandbox module with call-time instrument selection

**Approach**: `genetics.mr` is a sibling of `linemodels`/`enrichment`: composable pieces plus one orchestrator. `instruments(exposure, mode="cis"|"genome_wide", ...)` selects at call time — leads per credible set from `credible_sets`/`sql`, cis window from `gene_annotations_v`, MHC out, then r² pruning through **k neighbourhood calls** (`ld(v, window=3_000_000, r2_threshold=0.01, panel="finngen_r12", fields=["r","r2"])`, filtered to the candidate set, so the same k calls yield both the pruning r² and the signed r matrix for the kept set); pruning is mandatory when `datasets_v.pseudo_credible_sets`, and for deCODE the lead is the `cs_id` variant. Instruments missing from the outcome get a proxy from the same neighbourhood frames (r² ≥ 0.8, one batched `summary_stats` for all candidates). `harmonise()` joins on exact `chr:pos:ref:alt`, runs the AF cascade in the ruled order (outcome row `af` → exposure `aaf` → FinnGen annotation → gnomAD only for non-FinnGen exposure variants), and flags `most_severe` coding instruments. `estimate()` is the core; `run()` composes them, batches a one-exposure-to-many-outcomes screen as chunks of phenotypes with 4 in flight (the SDK docstring's own recipe), attaches the precomputed `colocalization` row when the outcome is FinnGen R14, derives the sample-overlap statement, and returns `{"estimate", "instruments", "sensitivity", "checks", "parameters"}` with every default echoed. Proteome-wide screens are refused with a message (120,000 LD calls do not fit), except the lead-only Wald form, which needs no LD and fits (1 `sql` ≤ 25,000 rows + 1-3 `summary_stats`).

**Affected files**:
- `/home/jkarjala/suite/genetics-mcp-server/src/genetics_mcp_server/sdk/mr.py` (new) — core, harmonise, instruments, run, checks
- `/home/jkarjala/suite/genetics-mcp-server/src/genetics_mcp_server/sdk/plots.py` — `mr()` figure (scatter with fitted lines, forest of Wald ratios via the existing `forest`, leave-one-out)
- `/home/jkarjala/suite/genetics-mcp-server/src/genetics_mcp_server/sdk/__init__.py`, `tools/executor.py`, `tools/definitions.py`, `skills/instructions/data_analysis.md`, `tests/test_sdk_import_closure.py`, `tests/test_code_execution_tools.py`, `docs/project-spec.md` — registration and documentation
- `/home/jkarjala/suite/genetics-mcp-server/tests/test_sdk_mr.py`, `tests/test_sdk_mr_live_shapes.py` (new; executor mocked as in `tests/test_ld_via_results_api.py`), `tests/fixtures/mr/` (new)
- `/home/jkarjala/suite/genetics-results-suite/sandbox/prune_venv.py`, `scripts/gen-sandbox-docs.py`, `sandbox/stubs/mr.pyi` (generated), `docs/chat-tool-reference.md`, `docs/project-spec.md`, `docs/code-execution-security.md` (the `sandbox/**` row fires), `scripts/mr-proving-ground.py` (new)
- `/home/jkarjala/suite/genetics-results-suite/configs/datasets.yaml` — optional `cohorts` field per dataset for the overlap statement (otherwise a module-local map that rots)

**Pros**:
- No data release, no new view, two repos; the user's v1 scope (per-target plus one-to-many) is exactly what fits the caps
- Selection parameters (window, threshold, r² prune) are the caller's; the result restates them
- The kill measurement needs nothing from this alternative; (b2) and (c) run through the real module on the local stack before any roll
- Signed r arrives for free from the pruning calls, so correlated-instrument IVW costs no extra requests

**Cons**:
- Per-target cost scales with candidate leads: deCODE's ~60 satellites per protein per chromosome mean ~60 LD calls (~12 s) before any estimate
- The one-to-many screen's server-side fan-out (1,423 files per `summary_stats` call) is unmeasured against the 120 s wall clock; chunk size is a guess until staging says otherwise
- Selection logic that runs in a sandbox on every call is hard to audit across releases: two sessions a month apart can get different instrument sets with no record other than the returned `parameters`

**Complexity**: Medium-High — ~1,000-1,300 lines core+harmonise+checks, ~500-700 lines selection/orchestration/batching, ~250 plot, ~900 tests, 12 files of registration/docs across two repos.

**Subtasks**:
1. Proving ground: local stack from the main checkouts (results-api daly profile with `finngen_r12`, db-api on `genetics_results`), `scripts/mr-proving-ground.py` skeleton, Data 8/9 (+ Zhou ST2, Zheng S1/S9) converted to checksummed TSV fixtures with an isolated interpreter
2. Crude (b): (b0) and (b1) checks, reporting r, the noise ceiling from both sides' SEs, and tier-1 directions — the kill ruling is applied here
3. R reference fixtures: Dockerfile, `reference.R` over Data 8 instruments for ~10 proteins plus a synthetic 50-variant polygenic table; outputs for every estimator
4. Estimator core in `sdk/mr.py`, matched to 1e-12 (deterministic) and Monte Carlo noise (bootstrapped)
5. `harmonise()` + AF cascade + `checks` frame, with mocked-executor tests
6. `instruments()`: lead rule, pseudo-CS handling, MHC, k-call pruning and r matrix, proxy search, pleiotropy count
7. `run()`: composition, one-to-many chunking, coloc attach, overlap statement, units
8. `genetics.plots.mr`
9. Registration (both repos), stub generation, docs, skill paragraph
10. (c) LDL-C → I9_CHD, PCSK9/IL6R/OAS1 anchors through the module on the local stack
11. Staging roll (results-api + sandbox), in-sandbox envelope measurement per-target and one-to-many; the screen number decides whether B is ever needed
12. Doc-accuracy pass over the epic's batched findings

---

### Alternative B: Precomputed instrument table (munge/db) plus a thin module

**Approach**: Instrument selection runs **once per release, offline**, where the LD files and sumstats are local and no cap applies. A munge job (`scripts/mr_instruments.py` + WDL) reads each exposure dataset's credible sets, takes the lead per set (cs_id variant for pseudo sets), classifies cis/trans against GENCODE, excludes MHC, prunes to r² < 0.01 against the FinnGen R12 matrices directly, stamps AF from the ruled source order, counts per-instrument trait multiplicity (Zheng tier 3), flags coding consequence, and writes two products: `mr_instruments` (one row per kept instrument per `(dataset, trait)`, with the selection rule and the reasons for every dropped lead) and `mr_instrument_ld` (signed r among the kept instruments of one trait). results-db gets schemas, views and a load script; `configs/datasets.yaml` gets the `tables:` entries so the schema docs and stubs are generated. `genetics.mr.instruments()` becomes one `sql()` over `mr_instruments_v` (2,000 proteins × ≤5 instruments fits the 25,000-row cap); the r matrix is one more `sql()`; no LD calls in the sandbox at all. `harmonise()`, `estimate()`, `run()` as in A. The natural extension is `mr_results_v`, the `colocalization_v` analogue — precomputed screens for the FinnGen R14 outcome family — which is the only way a proteome-wide screen ever fits the sandbox.

**Affected files**:
- `/home/jkarjala/suite/genetics-results-munge/scripts/mr_instruments.py`, `wdl/mr_instruments.wdl`, `docs/mr-instruments.md` (new)
- `/home/jkarjala/suite/genetics-results-db/schemas/mr_instruments.sql`, `mr_instruments_v.sql`, `mr_instrument_ld.sql`, `mr_instrument_ld_v.sql`, `scripts/load_mr_instruments.sh` (new)
- `/home/jkarjala/suite/genetics-results-suite/configs/datasets.yaml` (`tables:` entries, examples, partition/clustering), `docs/adding-datasets.md`, `docs/datasets-yaml-schema.md` if a field is new, generated `sandbox/schema/mr_instruments_v.md`, `scripts/check-example-scans.py` run
- Everything in A's mcp-server list, with `instruments()` reduced to a query and `run()` unchanged
- `docs/bigquery-dev-dataset.md` — the rehearsal dataset is where the first cut lands

**Pros**:
- Screens become one query plus one `summary_stats`; deCODE's satellites are pruned once instead of per call; the instrument set per release is auditable, versioned and the same for every user
- Conditionally independent signals (a COJO-style re-clump) are only realistic here, where the full LD files are local
- The selection job is the one place the deCODE data repairs (01-decode-pqtl-issues.md §4) can be applied without touching the loaded credible sets

**Cons**:
- Four repos and a data release cycle before the module has instruments; every new exposure dataset needs the job re-run and loaded
- Selection parameters are frozen per release (±500 kb vs ±100 kb, p-threshold, r² prune); a user who wants another window is back to call-time selection, so A's `instruments()` still has to exist as a fallback and the module carries two code paths
- The kill measurement must not wait for the table; if it does, the munge work is the thing that fails to retire risk early

**Complexity**: High — A's module minus ~300 lines of call-time LD, plus ~600-900 lines of munge pipeline and WDL, ~200 lines of schemas/load, the datasets.yaml entries with worked examples under the scan cap, and a BigQuery load into the rehearsal dataset first and `genetics_results` second.

**Subtasks**:
1. As A.1
2. As A.2 (the crude selection query here doubles as the prototype of the munge job's lead rule)
3. As A.3, 4. As A.4, 5. As A.5
6. `mr_instruments.py` first cut for UKB_PPP only, run locally against the R12 LD files, output loaded into the `genetics_dev` rehearsal dataset
7. Schemas, views, load script, datasets.yaml entries, generated schema docs, `check-example-scans.py`
8. `instruments()` as a query over the view, with A's call-time path kept for caller-supplied or custom-window instruments
9. `run()`, plot, registration, docs — as A.7-9
10. (b2) and (c) from the table on the local stack; extend the job to deCODE, FinnGen Olink ×3, Open Targets, NMR, FinnGen labs; production load
11. Staging roll and envelope measurement; decide `mr_results_v`
12. Doc-accuracy pass

---

### Alternative C: Estimator library plus explicit composition by the caller

**Approach**: `genetics.mr` is TwoSampleMR-shaped: pure functions over frames and nothing that chooses data. `harmonise(exposure, outcome)`, `af_check(frame, ...)`, `ld_matrix(variants, panel)` (the k calls), `prune(frame, r2)`, `wald/ivw/egger/median/mode/heterogeneity/loo/steiger`, `checks(frame, ...)` that evaluates whatever it is given, and `report(res)` that renders the STROBE-MR list from what was supplied. Instrument selection is a **documented recipe** — in the module docstring (which `list_capabilities` and the stub render) and in `data_analysis.md` — that the chat model composes: SQL for leads, `prune`, `summary_stats`, `harmonise`, an estimator. The replication harness is the same recipe written out, so it tests the recipe as much as the code.

**Affected files**:
- `/home/jkarjala/suite/genetics-mcp-server/src/genetics_mcp_server/sdk/mr.py` (new, smaller), `sdk/plots.py`, the registration set as in A
- `/home/jkarjala/suite/genetics-mcp-server/src/genetics_mcp_server/skills/instructions/data_analysis.md` — the recipe, considerably longer than one paragraph; `tools/definitions.py` run_analysis text
- Suite: `sandbox/prune_venv.py`, `scripts/gen-sandbox-docs.py`, `sandbox/stubs/mr.pyi`, `docs/chat-tool-reference.md`, `docs/project-spec.md`, `scripts/mr-proving-ground.py`

**Pros**:
- Smallest code; every function is a pure, fixture-testable transform; cis versus polygenic is the recipe's difference, not a branch
- Maximally flexible — uploaded instruments, Zhang's instruments verbatim, a user's own window — with no special mode
- The harness and the recipe are one artefact

**Cons**:
- The data-specific hazards (pseudo credible sets, deCODE's cs_id lead, the AF source order, MHC, one-sample statements, units) are enforced only where the model remembers to call the check; this repo's measured history (the `dir(genetics)` probing, prompt rules ignored wholesale) says prose rules are the ones that get skipped
- Two sessions compose the pipeline differently; "a predefined analysis that reproduces published MR" is not what the user gets, a toolbox is
- The one-to-many screen's chunking and the request budget are the model's problem every time

**Complexity**: Medium — ~900-1,100 lines core+harmonise+checks, ~150 lines of recipe prose, ~700 tests, same registration footprint; the complexity moves from code to the prompt surface, where it is not tested.

**Subtasks**:
1. As A.1, 2. As A.2, 3. As A.3, 4. As A.4
5. `harmonise`, `af_check`, `ld_matrix`, `prune`, `checks`, `report`
6. The recipe in the docstring and `data_analysis.md`; a golden test that the recipe's code block runs (pattern: the worked examples the schema docs gate)
7. Plot, registration, docs
8. (c) through the recipe on the local stack; staging roll and envelope measurement
9. Doc-accuracy pass

---

## 2. The decisions the alternatives actually disagree about

**D1. What is the unit of an instrument, and where is independence established?** A: a credible-set lead, pruned by r² at call time in the sandbox. B: a lead pruned (or re-clumped to conditionally independent signals) once per release in a munge job. C: whatever frame the caller built, checked but not chosen. *What settles it*: (b1) versus (b2) — if lead-only Wald already correlates with Zhang at the bar and pruned IVW moves estimates little, call-time pruning is cheap enough for per-target work and the table is only a screen optimisation; the staging envelope number then settles whether screens need B. *Cost to reverse*: A → B is additive (the `instruments()` selection code is the munge job's core, called by a different driver); B → A means re-adding call-time selection the module would have dropped; C → A is wrapping existing pieces. Going B first and finding the parameters wrong costs a data release per change.

**D2. Where does orchestration and hazard enforcement live — in a `run()` the module owns, or in a recipe the model follows?** A/B: code. C: prose. *What settles it*: the repo's own evidence on prompt-level rules, plus whether reviewers will accept "the model chose the instruments" as a predefined analysis. *Cost to reverse*: adding `run()` over C's pieces later is a day; retrofitting enforcement into sessions that already composed it wrong is not possible.

**D3. Correlated-instrument IVW in v1, now that r exists?** Only A gets it for free (the pruning neighbourhood calls return r); B precomputes it; C exposes `ld_matrix`. *What settles it*: the count of proteins whose kept cis set has > 1 instrument after pruning at 0.01 in (b2), and whether PD repair at the 0.01 floor changes SEs by more than a few percent on those. *Cost to reverse*: ~60 lines plus one fixture either way; nothing downstream depends on it.

**D4. Reference implementation: MendelianRandomization's formulas or TwoSampleMR's?** They agree on IVW and Egger but differ in defaults that the fixtures will pin — MendelianRandomization's "fixed with ≤ 3 variants, random with ≥ 4", its median bootstrap seed and iteration count, TwoSampleMR's `mr_steiger`/`get_r_from_bsen`, `mr_leaveoneout`, `mr_singlesnp` — and only MendelianRandomization has `correl=`. *What settles it*: fixture both on the same harmonised table and port MendelianRandomization for the estimates (it is the one with the correlated form) with TwoSampleMR for Steiger, leave-one-out and single-SNP conventions. *Cost to reverse*: numbers at the third decimal, never direction; the fixtures make any switch explicit.

**D5. Where the sample-overlap statement comes from.** A `cohorts` list per dataset in `configs/datasets.yaml` (one small registry change, fans out to the generated docs) versus a module-local map (an enumeration in code, which this repo's CLAUDE.md says rots). *What settles it*: the user — it is a registry schema decision. *Cost to reverse*: trivial, but a wrong statement printed with every estimate is the kind of rot nobody notices.

## 3. Recommendation

**Alternative A, built as C's core first**, with B deferred until the staging envelope measurement produces a number — and `instruments()` written from the start as a function of (leads frame, LD accessor) so the munge job of B can call the same code with a file-backed accessor if screens ever need it.

Reasons:
- The kill criterion is carried identically in all three and must run before any of them exists (subtasks 1-2); A is the only alternative whose remaining subtasks all run on the proving ground that exists today, with nothing gating (c) behind a data release.
- The user's v1 scope (per-target plus one-to-many) fits A's budget: ~10 + k requests per target, and the one-to-many screen is a chunking question the SDK's own docstring already answers; proteome-wide screens were ruled "after measuring", and the measurement is A's subtask 11.
- The hazards this investigation found are data facts (pseudo credible sets, the deCODE cs_id lead, the AF source order, the Olink 3K ascertainment, units) and belong in code that runs every time, not in a recipe; C's flexibility survives inside A because `instruments=` accepts a caller's frame and the pieces stay public.
- B's genuine advantages (auditable per-release instrument sets, offline re-clumping, the deCODE repairs) are advantages of a *data product*, and the right moment to build one is after (b2) shows what selection rule reproduces the literature — otherwise the table materialises a rule nobody has validated.

Three risks worth stating in the epic now:
1. **The kill number may be bounded below 0.9 by the outcome release, not by us.** Zhang's IIBDGC arm almost certainly used an earlier, smaller IIBDGC meta than our 2026 file (125,992 IBD cases), and the comparison set is mostly null proteins, so the correlation of two noisy estimates has a ceiling set by both sides' SEs. (b0) with Zhang's instruments verbatim isolates this, and the harness should print the noise ceiling and the tier-1 direction table beside r so the ruling is applied to the right number.
2. **Our UKB_PPP exposure betas are a FinnGen reanalysis at n = 42,853; Zhang's are Sun 2023's discovery subset at 33,995.** Expect a slope away from 1 on the per-protein estimates even with a perfect pipeline; direction and rank should hold.
3. **deCODE is the weak exposure platform in our data** (missing cis sets for OAS1 and IL18, ~60 satellites per strong protein, no AF, lead not always the max-PIP row); the module should treat it as "pseudo credible sets, pruning mandatory, AF from the outcome only" and the replication set should not depend on it.
