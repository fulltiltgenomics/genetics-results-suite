# The BigQuery rehearsal dataset

`genetics-results-suite-44g`. How to rehearse a BigQuery change before it touches live
data, and how to promote it once it is verified.

## Why this exists

**There is no development environment in this suite, and there never has been one that is
still standing.** A read-only survey on 2026-08-13 established:

- one GKE cluster (`gke_phewas-development_europe-west1-b_finngenie`), one kubeconfig
  context, no staging context;
- one GCP project. `phewas-development` is a historical name — `terraform.tfvars` sets
  `project_id = "phewas-development"`, the live results-api carries `DEPLOY_ENV=prod` and
  `LOG_SOURCE=finngenie_prod`, and the domains are `finngenie.finngen.fi` /
  `finngenie.fi`. **`phewas-development` IS production**;
- one application namespace, `genetics`. No `genetics-dev`;
- exactly three BigQuery datasets — `genetics_api_logs`, `genetics_chat_logs`,
  `genetics_results`. No dev copy of any of them. (**Since 2026-08-14 there is a fourth**,
  `genetics_dev` — a persistent **full-size** copy the local dev stack points db-api at,
  `genetics-results-suite-g08`, widened from its original chr22-only subset to all
  755,813,602 rows / 136.69 GB on 2026-08-18. It is a different object from the `genetics_results_dev`
  clone this document builds: that one is created and torn down around a single DDL
  rehearsal, this one stays. See [local-dev-vm.md](local-dev-vm.md), "The dev dataset".)
- the `daly` profile is a **second production brand** (its own project, region, domain,
  Keycloak realm and real Broad users), not a staging copy. It is not a canary.

Meanwhile five open beads are BigQuery DDL against the single live `genetics_results`
that the live db-api reads, and one of them is three irreversible ~27 GB `DROP TABLE`s.
This document plus `scripts/bq-dev-dataset.sh` is the rehearsal ground.

## What the script does

`scripts/bq-dev-dataset.sh` builds `genetics_results_dev` next to `genetics_results` in
the same project and region:

| step | mechanism | why |
|---|---|---|
| dataset | `bq mk --location=<source location>` | a clone cannot cross regions |
| base tables | `CREATE TABLE IF NOT EXISTS … CLONE …` | zero-copy and writable; starts at ~0 bytes, but see *Cost* — the **production** side diverging bills too |
| views | `CREATE OR REPLACE VIEW` over the **rewritten** production DDL | see below — this is the correctness crux |
| verification | re-reads every dev view out of BigQuery and greps for the source dataset | a view that slipped through would read production |

### The views are the thing that can silently ruin a rehearsal

Every view in `genetics_results` embeds a **fully-qualified**
`` `phewas-development.genetics_results.<table>` `` reference. Checked with `bq show
--format=prettyjson` on all 15 views on 2026-08-13: all are standard SQL
(`useLegacySql: false`), each has exactly one backticked three-part reference to its own
base table, and none has a bare/unqualified or cross-dataset `FROM`/`JOIN`.

So a view copied verbatim into `genetics_results_dev` **keeps reading the production
table** while the tables around it are dev clones. Every query through the view would
return production data, every DDL rehearsal against the dev base table would appear to do
nothing, and the rehearsal would look green while proving nothing at all.

The script therefore rewrites `<project>.<source>.` → `<project>.<dev>.` (and the two-part
`<source>.` form, which the `configs/datasets.yaml` worked examples use) before issuing
each `CREATE OR REPLACE VIEW`, refuses to create any view whose rewrite left a residue,
and then — separately, from what BigQuery actually stored —
`scripts/bq-dev-dataset.sh verify` reads every dev view back and fails loudly on any
remaining reference to the source dataset. **Run `verify` after every `create`, and again
after any rehearsal step that replaced a view.** It also checks that both datasets are in
the same location, that every source view exists in dev, and that every dev view's base
table was actually cloned.

Rewriting and detection are **one program in two modes**, deliberately: an earlier split
between a python rewriter and a `grep -E` check let per-part-backticked references through
both. Both modes accept backticks around the whole path (`` `p.d.t` ``), around each part
(`` `p`.`d`.`t` ``) or absent, and whitespace or newlines around the dots — all ordinary
BigQuery, and the rehearsal *requires* hand-written new views (94c's `hla_associations_v`
expand, 4h6.21's two new views, eyg's `credible_sets_v`), so the hand-written forms are the
ones that matter.

**Comments and string literals are left alone**, by both the rewrite and the check. The
match is otherwise purely textual, so without that a comment or a `WHERE source =
'genetics_results'` would be silently rewritten; none of the 15 live views has either, but
the ones written by hand during a rehearsal might. The consequence to know: a *comment*
naming the source dataset survives into the dev view and `verify` will not flag it (it
prints a note on stderr instead). A dataset reference cannot hide in a string literal
unless a view uses dynamic SQL, and none does.

What was actually run, through the script and not against the regex offline
(2026-08-13, after the `python3 -c` fix — before it the rewrite path emitted zero bytes and
so had never been exercised): all 15 live view definitions were pulled with `bq show
--format=prettyjson` and piped through `bq-dev-dataset.sh rewrite`. Every one produced
non-empty output exactly 4 bytes longer than its input (`_dev`), with exactly one
`genetics_results_dev` reference, zero residue, and byte-identical output when fed back
through the rewriter (idempotent). `create` was then run end to end against a stub `bq`:
the dry run printed 18 complete `CLONE` statements and 15 complete `CREATE OR REPLACE
VIEW` statements and reached its normal end with zero mutating calls; `--apply` issued
exactly those 33 statements, none of which names `genetics_results` on the target side.
Three views planted to read production — `` `phewas-development`.`genetics_results`.`asm_qtl` ``,
the same with a newline before the dataset part, and `` `genetics_results.mpra` `` — are
each rewritten correctly and each caught by `verify` (all three passed the old check).

### Which tables

**Wholesale, all 18 base tables, by default.** Clones are zero-copy, so cloning
everything costs the same as cloning three tables, and a partial clone leaves views over
missing tables — exactly the "looks fine until someone queries it" failure this dataset
exists to avoid. `--tables` and `--exclude` are there for the case where you deliberately
want a narrow dataset; `verify` will still tell you which views lost their base table.

Sizes as of 2026-08-13 (`bq show --format=prettyjson`, `numBytes`), 224.42 GB total:

| table | logical | rows |
|---|---|---|
| `gene_burden_results` | 74.32 GB | 327,363,096 |
| `open_chromatin` | 33.93 GB | 232,773,819 |
| `credible_sets_exp_rdtp` / `_rdvp` / `_drvp` | 26.96 GB each | 115,043,095 each |
| `credible_sets` | 23.75 GB | 115,076,906 |
| `coloc_credsets` | 5.30 GB | 30,460,655 |
| `variant_annotation` | 2.63 GB | 21,331,644 |
| `variant_effect` | 2.18 GB | 21,658,217 |
| the remaining 8 | < 0.5 GB each | — |

The three `credible_sets_exp_*` tables are 4h6.18's experiment tables and are what
`genetics-results-suite-4ci` proposes to drop. Clone them: it is free, and rehearsing
4ci's ordering is the whole point.

### Cost

Storage: a fresh dev dataset adds ~0 bytes. Storage is then billed for every block the
clone no longer shares with its base table — and **divergence is not only something the
rehearsal does.** A clone retains the blocks its base table supersedes, so *modifying or
dropping the production table* bills the clone for the superseded data just as surely as
writing to the clone does. Concretely, in this batch:

- `eyg` replaces production `credible_sets` (23.75 GB). At promotion the dev clone stops
  sharing storage with it and starts billing ~23.75 GB.
- `4ci` drops three production tables of 26.96 GB each. The same applies: the dev clones
  keep the dropped data alive and billable.

That is ~105 GB, roughly USD 2/month of europe-west1 active logical storage, starting at
promotion and running until teardown — and the cycle below says to tear down only when the
whole batch is done. So: **drop a table's dev clone as soon as its production counterpart
has been replaced or dropped** (`bq rm -f -t
phewas-development:genetics_results_dev.<table>`), unless that clone is still needed as a
verification reference. It is not part of the batch teardown; it is a step in the
promotion. The `eyg` rehearsal itself also materialises a ~27 GB table in dev, which is
another ~USD 0.55/month until teardown. Query: this is real BigQuery, so
scans bill at the normal rate. 4h6.18's measurement pass cost roughly USD 2–2.50 for
350–400 GB scanned; a rehearsal of the same shape costs the same again. Use
`--dry_run` for everything that only needs to be *validated*, and reserve executed
queries for the things dry-run cannot answer (below).

**The separate, permanent `genetics_dev` is now a real storage line of its own.** It is
not a clone and shares no blocks with production: widening it from the chr22 subset to
full size on 2026-08-18 took it from 0.57 GB to **136.69 GB**, roughly USD 2.70/month of
europe-west1 active logical storage, standing until someone deletes it. Reloading it
costs another ~134 GiB of scan (~USD 0.65) because it is `TRUNCATE` + `INSERT … SELECT`
from production, not a clone — see [local-dev-vm.md](local-dev-vm.md), "The dev dataset".

### `--dry_run` cannot verify clustering. This trap is load-bearing.

Recorded in 4h6.18: on a **freshly created** table, BigQuery's dry-run estimate ignores
clustering until the storage optimiser has processed it. Observed 4,492,232,401 B
dry-run against 517,406,337 B actually processed. A first benchmark pass using dry-run
showed the new tables as uniformly *worse*, entirely as an artefact.

So: dry-run proves **syntax and reference resolution**. Scan-byte claims need real
execution with `use_query_cache=False`. Assert the clustering **from
`INFORMATION_SCHEMA.COLUMNS.clustering_ordinal_position`**, before cutover, rather than
inferring it from scan numbers.

## The cycle

For every one of the five beads:

1. **Rehearse in dev.** Point the change at `genetics_results_dev`. For anything driven by
   `genetics-results-db/scripts/setup_bigquery.sh`, that is just
   `PROJECT_ID=phewas-development DATASET_ID=genetics_results_dev LOCATION=europe-west1
   ./scripts/setup_bigquery.sh` — the script already sed-substitutes
   `genetics_results` → `${PROJECT_ID}.${DATASET_ID}` into every `schemas/*.sql`, so no
   file needs editing to target dev. **Never pass `--recreate`:** it `bq rm`s *every*
   table in the dataset, not the one whose schema changed.
2. **`scripts/bq-dev-dataset.sh verify`.** Non-negotiable after any step that created or
   replaced a view.
3. **Run the bead's own acceptance test against dev**, with the dataset name rewritten:
   `scripts/bq-dev-dataset.sh rewrite < example.sql | …`.
4. **Promote**: run the identical statement against `genetics_results`, having changed
   nothing but the dataset name.
5. **Re-run the acceptance test against production.**
6. **Drop the dev clone of any table whose production counterpart step 4 replaced or
   dropped**, as soon as it is no longer a verification reference — a clone of a table that
   no longer exists in production bills for the whole table. See *Cost*.
7. **Tear down** the dataset when the whole batch is done: `scripts/bq-dev-dataset.sh
   teardown --apply --yes`. Not before — several of these beads verify against each other.

Expand / verify / contract, per bead, below.

## Ordering constraints

These are constraints between beads, verified against the beads themselves, not
preferences:

```
                 94c expand ──► ship 3 code artifacts ──► 94c contract
                                                          (breaks old readers)

  eyg (swap) ──► eyg verified ──┬──► 4ci  (DROP x3, irreversible)
                                ├──► 5p5  (datasets.yaml chr guidance)
                                └──► 4h6.30 (clustering docs, also needs 5p5)

  eyg verified ──► 4h6.20 (same treatment, 3 more tables)

  4h6.21 (backfills) — independent of all of the above
```

1. **`4ci` is strictly AFTER `eyg` is verified.** 4ci: *"Do NOT drop drvp until the real
   swap has landed and been verified — it is the reference for confirming the production
   table came out with the same pruning behaviour."* `credible_sets_exp_drvp` carries the
   chosen key order (`data_type, resource, variant, pos`) and is the only object that can
   confirm the production swap reproduced it. `credible_sets_exp_rdtp` and
   `credible_sets_exp_rdvp` may be dropped at any time.
2. **`94c`'s expand phase is safe; applying `schemas/hla_associations_v.sql` directly is
   the CONTRACT phase and breaks consumers.** 94c: *"Do NOT apply
   `schemas/hla_associations_v.sql` directly — it is the CONTRACT phase and applying it
   first breaks any consumer still using the old names."* The literal expand statement is
   in `docs/project-spec.md` § *HLA column rename rollout*; do not improvise it, because
   an improvised `SELECT *` plus five aliases silently drops the `CASE … AS resource`
   block that is the reason the view exists.
3. **`5p5` and `4h6.30` must NOT be merged early.** Both carry `datasets.yaml` guidance
   that is **correct today and becomes wrong only after `eyg` lands**. 5p5 measured, with
   real execution: `WHERE variant = '12:111446804:T:C'` scans 7,606,541,126 B pre-swap
   without a `chr` filter versus 367,715,852 B with one (a 20.8× saving, so line 237's
   "always add the chr filter" is exactly right today); post-swap the same pair is
   253,707,488 B versus 288,326,272 B — the `chr` filter becomes 13.6 % **worse**.
   Landing 5p5 early tells every agent to drop a filter that currently saves 20×, which is
   the precise production failure the note was written to prevent. 4h6.30 says the same
   for the same reason: *"Do this AFTER 4h6.18 and 5p5 land."*
4. **`4h6.20` after `eyg` is measured.** 4h6.20: *"Do this only after the credible_sets
   change has measured scan-byte improvement, so the approach is validated on one table
   before three more are migrated."*
5. **`4h6.21` is independent** — two new tables (`gene_expression`, `gene_disease`) that
   nothing else reads yet.

## Per-bead runbook

### `genetics-results-suite-94c` — `hla_associations_v` expand

**Premise, re-confirmed by dry-run on 2026-08-13 against production:**

```
$ bq --project_id=phewas-development query --dry_run --use_legacy_sql=false \
    "SELECT gene, allele, mlog10p, ... FROM genetics_results.hla_associations_v
     WHERE phenotype = 'K11_COELIAC' AND mlog10p > 7.3 ORDER BY mlog10p DESC"
Error in query string: Unrecognized name: mlog10p; Did you mean mlogp? at [1:146]

$ ... (the phenotypes_v join example) ...
Error in query string: Name mlog10p not found inside h at [1:206]
```

This is **already broken in production**, not a risk this work introduces: all five HLA
worked examples in `configs/datasets.yaml` and every example in
`sandbox/schema/hla_associations_v.md` fail today. Nothing executes them —
`scripts/test-sandbox-docs.py` only asserts the SQL's first line appears textually in the
rendered markdown — which is why it went unnoticed.

- **EXPAND** (in dev): the literal statement from `docs/project-spec.md` § *HLA column
  rename rollout*, with the dataset rewritten. It emits both spellings, so no state from
  here on is broken.
- **VERIFY** (in dev): `bq-dev-dataset.sh verify`, then dry-run **all five** worked
  examples through `bq-dev-dataset.sh rewrite`. All five must validate. That is the
  acceptance test, and it is the one thing no automated check covers.
- **PROMOTE**: the same statement against `genetics_results`, then re-dry-run the five
  examples against production. They must now all pass.
- **THEN, and only then, ship the three code artifacts** (mcp-server image, sandbox image,
  and `configs/datasets.yaml` via `scripts/deploy.sh` — `build.sh`/`rollout.sh` do **not**
  recreate the `datasets-config` ConfigMap). The expanded view serves both generations, so
  each rolls back with a plain `kubectl rollout undo` and no BigQuery action.
- **CONTRACT** (a separate, later window): apply the committed
  `schemas/hla_associations_v.sql`. Only after all three code artifacts are live.

### `genetics-results-suite-eyg` — the `credible_sets` clustering swap

The swap is prepared, not executed. The runbook is
`genetics-results-db/docs/credible-sets-clustering-swap.md` (723 lines) together with the
modified `schemas/credible_sets.sql` and `schemas/credible_sets_v.sql`. **Gap worth
knowing:** in the sibling checkout at `~/suite/genetics-results-db` that commit is on the
`worktree-db-only-architecture` branch only, so on the default branch the runbook file is
absent. Check out the right branch before you go looking for it.

- **EXPAND** (in dev): build `credible_sets_new` in `genetics_results_dev` per that
  runbook. Guard the 115M-row `INSERT` against running twice.
- **VERIFY** (in dev), in this order:
  1. assert the clustering from `INFORMATION_SCHEMA.COLUMNS.clustering_ordinal_position`
     on the **new base table** — `data_type, resource, variant, pos` — *before* cutover.
     The DDL is `IF NOT EXISTS`, so a leftover `credible_sets_new` from an aborted attempt
     would be reused with the wrong layout and pass every other check, and the runbook's
     own "disappointing scan numbers are expected, wait for the storage optimiser" note is
     exactly the explanation that would mask it;
  2. row count and a `variant`/`resource` mismatch count against the old table;
  3. view output schema — 22 columns, same names, order and types;
  4. scan bytes with real execution and `use_query_cache=False`, against
     `credible_sets_exp_drvp` as the reference. Not dry-run.
- **PROMOTE**: same statements against `genetics_results`. Keep the renamed-away old table
  as the rollback target and do not run a loader during the soak — a loader run `DELETE`s
  from the old table and then fails to re-append, destroying the rollback target.
- **AFTER VERIFICATION IN PRODUCTION**: unblock `5p5` (and then `4h6.30`) and `4ci`.
- **Permanent consequence to record when promoting:** `resource` becomes frozen at load
  time, so changing a `dataset_to_resource_rules` entry no longer takes effect by
  recreating the view — it needs a reload or an `UPDATE` backfill.

### `genetics-results-suite-4h6.20` — same treatment for three more tables

`coloc_credsets`, `exome_variant_results`, `asm_qtl`. Same expand/verify/contract shape as
`eyg`, one table at a time.

- **Only after `eyg` has measured a scan-byte improvement in production.**
- In dev, benchmark each against its cloned original before promoting.
- `gene_burden_results` is 74.32 GB / 327,363,096 rows — the largest object in the
  dataset. Size that migration separately; do not fold it into the same window.
- Note `setup_bigquery.sh` auto-creates a table for every `schemas/*.sql` that is not
  `*_v.sql`, so any scratch table must never gain a schema file.

### `genetics-results-suite-4h6.21` — gene expression and gene-disease backfills

Two new tables. Independent of the other four; rehearse in dev mainly to confirm the
loader, the schema and the view all land, and that `verify` still passes afterwards (the
new views must reference `genetics_results_dev`, and this is the easiest place to get that
wrong because the view SQL is being written from scratch).

Promotion also needs the `VIEWS` allowlist in `genetics-results-api`'s `api/main.py` and a
`configs/datasets.yaml` entry — see `docs/adding-datasets.md`.

### `genetics-results-suite-4ci` — drop the three experiment tables

**The only irreversible bead in practice. ~27 GB each.** BigQuery's time travel on this
dataset is `maxTimeTravelHours: 168` — seven days — and it *does* cover a dropped table:
`UNDROP TABLE` (or `CREATE TABLE … AS SELECT … FROM t FOR SYSTEM_TIME AS OF …`) restores
one within the window. **Do not plan on it.** It only works while no table of that name has
been created since — `setup_bigquery.sh` recreating an empty table of the same name burns
the recovery — the window is seven days from the drop and nothing warns you when it
closes, and the restored table is the one you must then re-verify. Treat the drop as
irreversible and rely on the ordering below; `UNDROP` is an emergency, not a rollback plan.

- `credible_sets_exp_rdtp` and `credible_sets_exp_rdvp`: droppable at any time.
- `credible_sets_exp_drvp`: **only after `eyg` is verified in production.** It is the
  verification reference.
- There is nothing to rehearse about a `DROP` beyond the ordering, so the dev value here is
  as a checklist gate: do not run this until the `eyg` verification queries have been run
  and their output recorded on the bead.
- These tables must never gain `schemas/*.sql` files, or `setup_bigquery.sh` would
  recreate them on the next production setup run.

## Do the services need pointing at the dev dataset?

**No, and deliberately do not add the plumbing.** All five beads are SQL against BigQuery
plus SQL acceptance tests; running the SQL directly, with the dataset name rewritten, is
the whole rehearsal. Nothing in the rehearsal needs a pod.

What is configurable today, for the record:

| consumer | dataset name comes from | dev-pointable? |
|---|---|---|
| `genetics-results-db` (db-api) | `DATASET_ID` env var | yes, in code — but see below |
| `k8s/deployments/db-api.yaml` | `value: "genetics_results"`, a **literal** | **no** — unlike the monitor CronJob, which uses `${BQ_DATASET}` |
| monitor CronJob | `${BQ_DATASET}`, `envsubst`-ed by `deploy.sh` (default `genetics_results`) | yes |
| `genetics-results-db/scripts/setup_bigquery.sh` | `PROJECT_ID` / `DATASET_ID` / `LOCATION` env vars | yes — this is how DDL is applied to dev |
| `configs/datasets.yaml` worked examples | the dataset name is **written into the SQL text** | no — must be rewritten textually |

Two findings rather than changes:

1. `k8s/deployments/db-api.yaml` hardcodes `DATASET_ID: "genetics_results"` even though
   `deploy.sh` already exports `BQ_DATASET` (line 106) and already lists `${BQ_DATASET}`
   in its `envsubst` whitelist (line 442). Making db-api dev-pointable is a one-token
   manifest change, if a future rehearsal ever needs a service in the loop. It is **not**
   needed for these five beads, so it has not been made.
2. The `configs/datasets.yaml` worked examples embed the dataset name in the SQL, so no
   env var can redirect them — hence `bq-dev-dataset.sh rewrite`. This is also why 94c's
   acceptance test has to go through the rewriter.

## Guards, and what they refuse

`scripts/bq-dev-dataset.sh` will not run at all if the target dataset is
`genetics_results`, `genetics_api_logs` or `genetics_chat_logs`, or if the target name has
no `dev` segment (`^dev$`, `^dev_`, `_dev_`, `_dev$`), or if target and source are the
same. There is no override flag; a rehearsal dataset that could be production is not a
rehearsal dataset.

Everything destructive is opt-in twice: **dry run is the default**, `--apply` executes,
and `teardown` additionally needs `--yes` *and* the dataset name typed at a tty — the same
shape of guard `setup_bigquery.sh --recreate` uses. `create` is idempotent: existing dev
objects are skipped, not overwritten, so a second run cannot silently discard a
half-finished rehearsal. `--refresh --yes` is how you deliberately re-sync from
production.

One condition on that idempotence: "does the dev dataset/object exist?" is answered by
`bq show`/`bq ls` with `2>/dev/null … || true`, because *absent* is a normal answer. A
**transient** failure — expired credentials, a revoked permission, a 5xx — is therefore
indistinguishable from absent, and an existing dev dataset would look empty: the views
would be `CREATE OR REPLACE`d rather than skipped, discarding rehearsal state in them. The
clones are `CREATE TABLE IF NOT EXISTS` and survive. The blast radius is dev-only, but if
`create` reports objects you know exist as missing, stop and fix the credentials rather
than letting it "repair" the dataset.

## First run

```bash
scripts/bq-dev-dataset.sh check                 # read-only; touches nothing
scripts/bq-dev-dataset.sh create                # prints the plan, executes nothing
scripts/bq-dev-dataset.sh create --apply
scripts/bq-dev-dataset.sh verify                # must print VERIFIED before you rehearse
```
