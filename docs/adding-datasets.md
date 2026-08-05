# Adding a new dataset

This guide explains how to add a new dataset (or new data for an existing one) to the
genetics results platform, for one or both deployment profiles (`finngen` and `daly`).
It is written for both humans and LLM agents. Follow it top to bottom; the checklist at
the end is the short version.

> Golden rule: a dataset is only "done" when it is consistent across **all** of these
> layers — otherwise the API serves it but the agent can't describe it, or BigQuery
> mislabels its `resource`, or one profile works and the other doesn't.

## 1. Mental model: two config layers

There are two distinct configuration layers. Knowing which one a change belongs to is
the single most important thing.

| Layer | Lives in | Holds | Delivered to runtime by |
|-------|----------|-------|--------------------------|
| **Canonical `datasets.yaml`** | `genetics-results-suite/configs/datasets.yaml` | Profile-independent `resources`, BQ `tables` metadata, `dataset_to_resource_rules`; and the per-profile **dataset registry** (`profiles.<profile>.datasets`) — descriptions, versions, sample sizes, `pseudo_credible_sets`, etc. | `deploy.sh` creates the `datasets-config` ConfigMap from it and mounts it into the **results-api** and **db-api** pods at `/app/configs/datasets.yaml`. No image rebuild needed. |
| **results-api product configs** | `genetics-results-api/app/config/profiles/<profile>/*.py` | The **actual GCS file paths** (`credible_sets.py`, `summary_stats.py`, `exome_results.py`, `gene_based_results.py`, `expression.py`, `coloc.py`, …) and the exact-dataset-name → `(resource, version)` map (`common.py`'s `dataset_to_resource`). | Baked into the **results-api Docker image at build time** (`build.sh` clones the repo from GitHub). Changes require **rebuild + rollout** of results-api, and must be committed/pushed first. |

Consequences:

- Adding **metadata** about a dataset (description, version label, pseudo flag, sample
  sizes, a new resource label) → edit `datasets.yaml`, then `sync-datasets.sh`. It reaches
  the pods on the next `deploy.sh` via the ConfigMap.
- Adding/changing **where the data actually lives** (GCS paths) or how a `dataset` column
  value maps to a resource for range queries → edit `genetics-results-api`, then rebuild
  and roll out the results-api image.
- Most new datasets touch **both** layers.

## 2. Repos involved

- **genetics-results-suite** (this repo) — canonical `datasets.yaml`, deploy/build/sync
  scripts, docs. Source of truth for metadata.
- **genetics-results-api** — serves results from GCS files over HTTP. The agent's MCP
  credible-set / summary-stat / exome tools all call this API, so this is what makes a
  dataset **searchable and agent-reachable** for those tools. Holds the GCS paths.
- **genetics-results-db** — db-api (BigQuery proxy for the agent's `query_bigquery` /
  schema tools) and the BQ view SQL (`schemas/*.sql`). Reads `datasets.yaml` at runtime
  for resource/table metadata; the `*_v` views map the `dataset` column → `resource`.
- **genetics-mcp-server** — the agent/backend and standalone MCP server. Has **no
  hardcoded dataset paths**; it reaches everything through results-api and db-api, so it
  usually needs **no changes**.
- **genetics-results-browser** — the web frontend. Discovers resources/datasets from
  db-api `/schema` (see `src/features/chat/schemaApi.ts`), so a new dataset surfaces
  **automatically with no code change**. The one exception is display naming: if the raw
  `dataset` id reads poorly or ambiguously in the result tables, add a frontend label
  override to `DATASET_LABEL_OVERRIDES` in `src/features/table/utils/tableutil.tsx`
  (e.g. `UKB_PPP` → "UKBB PPP (Olink 3K)"). Optional, and only for display clarity.

Both `genetics-results-api` and `genetics-results-db` keep a committed copy of
`datasets.yaml` under `configs/`, refreshed by `scripts/sync-datasets.sh` (and by
`deploy.sh`, best-effort). These committed copies are for local dev and CI drift checks;
the deploy-time ConfigMap is authoritative at runtime.

## 3. First decide: new resource, or new data for an existing resource?

A **resource** is a data source (e.g. `finngen`, `pgc`, `ibd_gwas`). A **dataset** is a
specific product within a resource (e.g. `pgc_scz`, `pgc_bip` both live under resource
`pgc`). Multiple datasets can share a resource.

- If the data belongs to a **source already present** (e.g. another FinnGen release, or —
  as in the IBD pseudo-CS example below — credible sets for a GWAS whose summary stats are
  already served), **reuse the existing resource and ideally the existing dataset id**.
  Do not invent a parallel resource. Check `resources:` and the `profiles.*.datasets`
  registry in `datasets.yaml` first.
- If it is a genuinely new source, add a new `resources:` entry (shared, profile-independent)
  with a `label`, `description`, and `aliases` so the agent can map user phrasing to the
  resource filter.

Keep related datasets tidy: if summary stats, credible sets, and exome results all come
from the same study, they should share one resource and consistent dataset ids/phenotype
codes across the product configs.

## 4. Add the dataset metadata (`datasets.yaml`)

In `genetics-results-suite/configs/datasets.yaml`:

1. **(If new source)** add a `resources.<resource_id>` block.
2. Under **each profile you target** (`profiles.finngen.datasets` and/or
   `profiles.daly.datasets`), add a `<dataset_id>:` entry. The two profiles share
   identical definitions **except** `metadata_file` GCS paths:
   - finngen: `gs://finngen-commons/results_api_data/...`
   - daly: `gs://daly-genetics-results/...`
   Required fields: `resource`, `version`, `description`, `author`, `publication_date`,
   `data_type`, `trait_type`. Optional: `n_samples`/`n_cases`/`n_controls`/`n_phenotypes`,
   `phenotypes:` (for small fixed-phenotype sets), `metadata_file`, `metadata_harmonizer`,
   `pseudo_credible_sets: true` (if credible sets are pseudo, not formally fine-mapped),
   `collection: true` + `subdataset_id_field` (for large sub-study collections).
   See `docs/datasets-yaml-schema.md` for the full field reference and enums.
3. **(If the data goes into BigQuery)** add a `dataset_to_resource_rules` entry mapping the
   BQ `dataset` column value to the resource, unless the default `* → LOWER(dataset)`
   fallback already produces the correct resource id. It does **not** when the column value
   lowercased differs from the resource id (e.g. `IIBDGC` → would give `iibdgc`, but the
   resource is `ibd_gwas`, so an explicit rule is required). Put more specific patterns
   before broader ones; the `*` fallback stays last.

Then propagate to the sibling repos:

```bash
./scripts/sync-datasets.sh
```

## 5. Add the data paths (`genetics-results-api`)

Edit the product config(s) under `app/config/profiles/<profile>/` for **each profile**.
Each entry's `id` is what `dataset_products()` keys on, so a matching `id` is what makes
the `/datasets` endpoint advertise the product (this is how the **agent and search find
it**). `dataset_id` links the entry back to the `datasets.yaml` registry; `resource` is
the API-side resource grouping.

- **Summary statistics** → `summary_stats.py`, `summary_stats_data_files` list. One entry
  per phenotype with `"file"`, `"phenotype"`, and a `"column_mapping"`.
- **Credible sets** → `credible_sets.py`, `data_files` list. A `"cs"` block with a
  `"prefix"` (per-phenotype individual files), a `"suffix_95"`, and an `"all_cs_file"`
  (combined tabix-indexed file used for gene/region/variant range queries). QTL datasets
  may also set `"all_cs_qtl_file"`; FinnGen-style sets add a `"stats_file"`.
- **Exome variant results** → `exome_results.py`, `exome_data_files`. An `"exome"` block
  with either an `"all_exome_file"` (combined) and/or a `"prefix"` + `"suffix"` for
  per-phenotype files.
- **Gene-based / burden results** → `gene_based_results.py`, `gene_based_data_files`. A
  `"gene_based"` block with a `"file"` (combined, gene-locus indexed, backs
  `/gene_based/{gene}` across all traits) plus a `"prefix"` + `"suffix"` for the
  unfiltered per-trait files that `/gene_based_results_by_phenotype/{resource}/{trait}`
  streams. The per-trait files are also what BigQuery's `gene_burden_results` is loaded
  from. For genebass the combined file holds the mlog10p_burden > 4 hits only.
- **Expression / coloc / chromatin / gene-disease** → the correspondingly named module.
- **Open chromatin / variant effect** — unlike a plain new dataset (which only needs a
  `datasets.yaml` entry plus an existing product config), these are two **new products**.
  Each gets its own new results-api tabix vertical (`open_chromatin.py` / `variant_effect.py`,
  cloned from the chromatin_peaks vertical) plus a new BigQuery view (`open_chromatin_v` /
  `variant_effect_v`) — position-indexed and variant-indexed respectively, `data_type:
  open_chromatin` / `variant_effect` and both `trait_type: null`. Those modules/views are
  created by the sibling results-api and results-db tasks in this epic; this repo only adds
  the `datasets.yaml` registry entries and any `dataset_to_resource_rules`.
- **MPRA** (`siraj_mpra`, Siraj et al. 2026) reused this same new-vertical + new-view pattern:
  a measured MPRA functional annotation with its own results-api tabix vertical (`mpra.py`) and
  BigQuery view (`mpra_v`, variant-indexed, LONG one row per variant × cell line), `data_type:
  mpra` and `trait_type: null`. As above, this repo only owns the `datasets.yaml` entries and
  `dataset_to_resource_rules`.

### The shared-combined-file + per-row resource filter (important for credible sets)

Several datasets can share one combined file (e.g. the external pseudo-CS live in one
`ext_pseudo/EXT_*_pseudo_credible_sets.*.tsv.gz`). For range/variant queries the results-api
tabixes each unique `all_cs_file` once and then **filters rows by resource**. The per-row
resource is computed from the row's `dataset` column via `dataset_to_resource` in
`app/config/profiles/<profile>/common.py`. **If a shared-file `dataset` value is missing
from that map it resolves to `unknown` and the rows are silently dropped from gene/region/
variant queries** (per-phenotype lookups via the individual `prefix` files still work).
So when adding a dataset whose rows live in a shared combined file, add its `dataset`
column value to `dataset_to_resource` in **both** profiles, e.g. `"IIBDGC": ("ibd_gwas", "2026")`.

After editing, sanity-check the import for both profiles:

```bash
cd ../genetics-results-api
for p in finngen daly; do CONFIG_PROFILE=$p python3 -c "
from app.services import config_util
print('$p', config_util.dataset_products('<dataset_id>'))"; done
```

## 6. BigQuery views (`genetics-results-db`)

Only relevant if the data is loaded into BigQuery (for the agent's raw `query_bigquery`
and schema tools). The `*_v` views derive the `resource` column from the `dataset` column
via the same `dataset_to_resource_rules` in `datasets.yaml`.

After changing the rules, regenerate and verify the view SQL:

```bash
cd ../genetics-results-db
python3 scripts/generate_resource_sql.py generate credible_sets_v   # prints the CASE fragment
python3 scripts/generate_resource_sql.py lint                       # must report "All views match."
```

If `lint` reports a mismatch, edit the affected `schemas/*_v.sql` so the `CASE` block matches the
generated fragment. The resource-mapped views are `credible_sets_v`, `colocalization_v`,
`coloc_credsets_v`, `exome_variant_results_v`, `gene_burden_results_v`, `asm_qtl_v`,
`open_chromatin_v`, `variant_effect_v`, `mpra_v` and `peak_to_gene_v` (the script's `ALL_VIEWS`).

Apply the schema/view changes to BigQuery with `scripts/setup_bigquery.sh` (creates tables
`IF NOT EXISTS` and re-applies every `*_v` view via `CREATE OR REPLACE` — no data loss;
`--recreate` drops and rebuilds tables and **deletes data**). To apply just one changed
view, pipe its `schemas/<view>.sql` through `bq query` after substituting the
`genetics_results` placeholder with `<project>.<dataset>`.

Loading the actual rows into the base tables is a separate step — **not** done by
`deploy.sh`. The low-level loader is `scripts/load_data.py`; per-data-type wrapper scripts
drive it (e.g. `load_pseudo.sh` for the shared external/meta pseudo credible-set bundle,
`load_credsets_coloc.sh`, `load_genebass_variants.sh`, `load_gene_burden_extra.sh`, …). Each
wrapper deletes the dataset's existing rows and re-appends from GCS, so adding a new
`dataset` value (e.g. `IIBDGC`) means adding it to the relevant wrapper's GCS file list and
its delete-before-load set. Set `PROJECT_ID`/`DATASET_ID`/`GCS_BUCKET`/`GCS_PREFIX` per
profile (finngen vs daly buckets).

## 7. Deploy

1. **datasets.yaml-only changes** (metadata, pseudo flag, a new mapping rule): run
   `./scripts/deploy.sh`. It syncs the sibling repos (best-effort), rebuilds the
   `datasets-config` ConfigMap, and restarts results-api / db-api so they pick it up.
2. **results-api path/code changes**: commit and push `genetics-results-api`, then
   `./scripts/build.sh results-api` (or `build-all.sh`) to build+push a new image, then
   `./scripts/deploy.sh` (or `./scripts/rollout.sh results-api <tag>`). `deploy.sh` alone
   does **not** rebuild images.
3. **BigQuery view changes**: apply the updated `schemas/*_v.sql` to BigQuery and load the
   base-table rows via the db repo's pipeline.

## 8. Verify

- `GET /api/v1/datasets` lists the dataset with the expected `products` (e.g.
  `"credible_sets": true`) and `pseudo_credible_sets` flag.
- Hit the relevant endpoint (e.g. `/api/v1/credible_sets_by_gene/<gene>`,
  `/api/v1/summary_stats/<resource>/<data_type>?variants=...&phenotypes=...`, or
  `/api/v1/summary_stats_by_region/<resource>/<data_type>/<chr:start-end>?phenotypes=...`)
  and confirm rows come back for both profiles.
- Ask the agent for the data by gene/phenotype and confirm it finds it (it goes through
  results-api).
- If in BigQuery: `SELECT DISTINCT resource FROM <view> WHERE dataset = '<value>'` returns
  the intended resource id.
- The monitor CronJob (`scripts/monitor/`) checks dataset accessibility and BQ resource
  coverage and will flag drift between expected and actual resources.

## 9. Worked example: IIBDGC IBD/UC/CD pseudo credible sets

Context: the `ibd_gwas` dataset (IIBDGC IBD/UC/CD GWAS meta-analysis) already served
summary statistics. New **pseudo** credible sets were produced for the same three
phenotypes, delivered under `credible_sets/ext_pseudo/` (combined
`EXT_20260610_pseudo_credible_sets.*.tsv.gz` plus per-phenotype files in
`ext_pseudo/individual/iibdgc/{IBD,UC,CD}.report.out.pseudo_cs.*.tsv`), replacing the older
`credible_sets/ext/` location and adding IBD/UC/CD alongside the existing COVID/PGC/GP2.

Changes made (reuse the existing `ibd_gwas` resource/dataset — no new resource):

1. `datasets.yaml`: added `pseudo_credible_sets: true` to `ibd_gwas` in both profiles; added
   a `dataset_to_resource_rules` rule `IIBDGC → ibd_gwas` (lowercase fallback would wrongly
   give `iibdgc`); `sync-datasets.sh`.
2. `genetics-results-api` (both profiles): in `credible_sets.py` repointed the four existing
   external datasets from `ext/` to `ext_pseudo/` and bumped the combined filename, and
   added a new `ibd_gwas` credible-sets entry (prefix `ext_pseudo/individual/iibdgc/`, the
   shared combined `all_cs_file`); in `common.py` added `"IIBDGC": ("ibd_gwas", "2026")` to
   `dataset_to_resource` so shared-combined-file rows attribute correctly in range queries.
3. `genetics-results-db`: updated `credible_sets_v.sql`, `colocalization_v.sql`,
   `coloc_credsets_v.sql` so the `resource` CASE includes `WHEN dataset = 'IIBDGC' THEN
   'ibd_gwas'` (verified with `generate_resource_sql.py lint`). Loading the IIBDGC rows into
   the BQ `credible_sets` table remains a separate data-pipeline step.
4. genetics-mcp-server: no change (reaches data via results-api / db-api).

Result: `dataset_products("ibd_gwas")` reports `credible_sets: true`, the agent's
credible-set tools (which call results-api) return IBD/UC/CD pseudo CS, and the BQ views
label IIBDGC rows as `ibd_gwas` once loaded.

## 10. Worked example: PGC SCZ published fine-mapping next to its pseudo credible sets

Context: `pgc_scz` already served pseudo credible sets from the shared `ext_pseudo/` file. The
published FINEMAP 95 % credible sets (Trubetskoy et al. 2022, ST11a) were munged into the
standard credible-set format and are now served **alongside** them, not in place of them, under
the same `pgc` resource.

Changes made (new dataset, existing resource, own file — not part of the shared `ext_pseudo` one):

1. `genetics-results-munge`: `scripts/munge_pgc_scz_finemap.{py,sh}` plus
   `docs/pgc-scz-finemapping.md`. Output
   `credible_sets/pgc_scz_finemap/2022/PGC_SCZ_2022_credible_sets.tsv.gz` with per-trait files
   under `individual/` and `credible_set_stats.tsv`.
2. `datasets.yaml`: `pgc_scz_finemap` dataset in both profiles (no `pseudo_credible_sets` flag —
   these are real). A `PGC_SCZ%` rule was needed because the existing `PGC` rule is an **exact**
   match and would not have caught `PGC_SCZ_2022`; `sync-datasets.sh`.
3. `genetics-results-api` (both profiles): `credible_sets.py` entry `pgc_scz_finemap` with its own
   `all_cs_file`, `prefix`/`suffix_95` (`.FINEMAP.munged.tsv`) and `stats_file`; `common.py`
   `"PGC_SCZ_2022": ("pgc", "2022")`.
4. `genetics-results-db`: the `PGC_SCZ%` branch added to `credible_sets_v.sql`,
   `colocalization_v.sql` and `coloc_credsets_v.sql` (`generate_resource_sql.py lint`), and the
   file added to `load_credsets_coloc.sh` — **not** `load_pseudo.sh`, since it is real
   fine-mapping — with `PGC_SCZ_2022` added to that script's surgical `DELETE` list.

The point of interest: two datasets under one resource now carry credible sets for the same trait
code (`SCZ`), one pseudo and one fine-mapped. That is intended, but it means a consumer that wants
only genuine fine-mapping must filter on `dataset`, not on `resource`.

## Checklist

- [ ] Decide: new resource or reuse existing? (`resources:` + registry in `datasets.yaml`)
- [ ] `datasets.yaml`: dataset entry under each target profile (mind `metadata_file` bucket).
- [ ] `datasets.yaml`: `dataset_to_resource_rules` entry if BQ-bound and the lowercase
      fallback is wrong; set `pseudo_credible_sets: true` for pseudo CS.
- [ ] `./scripts/sync-datasets.sh`.
- [ ] genetics-results-api: product config path entry for each profile (matching `id`).
- [ ] genetics-results-api: `common.py` `dataset_to_resource` entry if the data shares a
      combined credible-set file (per-row resource attribution).
- [ ] genetics-results-db: regenerate/verify `*_v.sql` (`generate_resource_sql.py lint`),
      apply views + load BQ rows if BQ-bound.
- [ ] genetics-mcp-server: usually nothing.
- [ ] genetics-results-browser: usually nothing (API-driven); add a
      `DATASET_LABEL_OVERRIDES` entry only if the raw dataset id needs a clearer label.
- [ ] Build + roll out results-api if its configs changed; `deploy.sh` for datasets.yaml.
- [ ] Verify `/datasets`, the data endpoint, the agent, and (if applicable) the BQ view —
      for **both** profiles.
