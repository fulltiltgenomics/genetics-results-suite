# datasets.yaml Schema Reference

Canonical schema for `configs/datasets.yaml` -- the single source of truth for dataset
and resource definitions consumed by all services in the genetics results platform.

## Top-level structure

```yaml
# shared across all profiles
resources: { ... }
tables: { ... }
dataset_to_resource_rules: [ ... ]

# per-profile
profiles:
  finngen: { ... }
  daly: { ... }
```

## Section: `resources`

Profile-independent metadata for each resource (data source). Used by db-api to expose
human-readable labels, descriptions, and aliases so LLM agents can map user intent to
the correct `resource` filter value.

```yaml
resources:
  <resource_id>:          # string, lowercase, matches `resource` column in BQ views
    label: string         # required -- human-readable display name
    description: string   # required -- one-line description of the resource
    aliases: [string]     # optional (default []) -- alternate names users might use
```

### Collection resources

Some resources represent large collections of sub-studies (e.g. eQTL Catalogue with
hundreds of QTD* datasets). These are declared with extra fields:

```yaml
resources:
  eqtl_catalogue:
    label: "eQTL Catalogue"
    description: "..."
    aliases: []
    collection: true                # flags this as a collection resource
    collection_id_prefix: "qtd"     # lowercase prefix pattern for dataset IDs
    collection_data_types: ["eQTL", "sQTL"]  # data types in the collection
```

**Consumer**: db-api (`_RESOURCE_METADATA`, `_COLLECTION_RESOURCE_PREFIXES`)

## Section: `tables`

Profile-independent metadata for BigQuery view tables. Contains descriptions,
column descriptions, example queries, and categorical column configuration.

```yaml
tables:
  <table_name>:                    # e.g. "credible_sets_v"
    exposed: true                  # optional (default: NOT exposed) -- see below
    description: string            # required -- table-level description

    columns:                       # optional -- per-column descriptions
      <column_name>: string        # description text; overrides BQ field descriptions

    column_types:                  # required when `columns` is present -- BigQuery types
      <column_name>: string        # data_type verbatim from INFORMATION_SCHEMA.COLUMNS

    examples:                      # optional -- example SQL queries for agents
      - description: string        # what the query demonstrates
        sql: string                # the SQL query text; name views BARE (`FROM <view>`) --
                                   # no project/dataset prefix and no backticks around the
                                   # name (see below)

    categorical_columns:           # optional -- low-cardinality columns exposed in /schema
      <column_name>: string | null
        # null: flat list of distinct values
        # string: values depend on this parent column (e.g. "resource")
```

### Field details for `tables.<table>.examples`

`sql:` must name every view by its **bare**, unbackticked name — `FROM credible_sets_v`,
never `FROM genetics_results.credible_sets_v`. These examples
are copied verbatim into `sandbox/schema/<view>.md` and are what a sandboxed agent imitates,
so the convention here becomes the convention in generated queries. db-api sets
`default_dataset` on the job configs it submits, so **BigQuery** resolves a bare name against
whichever dataset that binary serves, which is what lets one binary serve `genetics_dev` and
`genetics_results` with no change to the SQL. `genetics-results-suite-4h6.53` replaced an
earlier `_qualify_tables` regex rewrite in db-api with this; **the failure mode that
description carried no longer exists** — backticking a bare name used to defeat the rewrite
and reach BigQuery unqualified, whereas BigQuery resolves `` `credible_sets_v` `` against the
default dataset like any other identifier. Backticks stay out of the examples for
consistency, not for correctness. The one way still to get this wrong is silent here and
loud later: **qualifying the name yourself pins the example to one dataset**, and the
allow-list compares fully-qualified ids, so an example carrying the wrong project or dataset
is refused rather than quietly reading the wrong table.

### Field details for `tables.<table>.exposed`

An entry in this block **documents** a view: `scripts/gen-sandbox-docs.py` emits one
`sandbox/schema/<view>.md` for every entry, exposed or not. `exposed: true` is the separate,
explicit decision that makes the view **reachable** through db-api's `/query`, `/schema` and
`/tables/{name}/sample`, and that puts its `dataset` values under the registry cross-check
(`genetics-results-db`'s `scripts/live_dataset_scope.py`).

It defaults to **not exposed**, and only the literal `true` counts: an allow-list must not
widen because someone documented a table or mistyped the flag. db-api derives its `VIEWS`
from it (`api/yaml_loader.py::load_views`) and refuses to start when the result is empty;
`genetics-results-db/tests/test_exposed_views.py` pins the resulting set by name, so both a
widening and a narrowing fail that test. Nothing on the deploy path re-checks it.

**Consumers**: db-api (`VIEWS` and the `/query` allow-list), `live_dataset_scope.py`.

### Field details for `tables.<table>.column_types`

A parallel block to `columns`, keyed by the same column names, whose value is the column's
BigQuery type — **not** a description. It exists because the sandbox schema documentation
(`sandbox/schema/<view>.md`, generated by `scripts/gen-sandbox-docs.py` and baked into the
code-execution image) is the only description of these views a sandboxed agent gets, and a
column with no type there produces SQL that cannot work: `chr` is `INT64`, so both
`chr = 'chr5'` and `chr = '5'` are errors, and `gene_group_ids` is `ARRAY<INT64>`, so it
needs `UNNEST` rather than `=`.

Rules:

- **Every column in `columns` must have an entry**, and no entry may name a column
  `columns` does not document. Both directions are enforced by
  `scripts/test-sandbox-docs.py`, which gates `scripts/build.sh`; `gen-sandbox-docs.py`
  itself refuses to render a view with an untyped column rather than emitting a blank cell.
- The value is `data_type` **copied verbatim** from
  `genetics_results.INFORMATION_SCHEMA.COLUMNS`, not hand-written. That is the spelling
  BigQuery itself uses, including the parameterised forms (`ARRAY<STRING>`,
  `ARRAY<INT64>`, `STRUCT<...>`). `INT`, `float` or prose are rejected by the harness's
  grammar check.
- It is a **separate block rather than a widening of `columns`** to a
  `{description, type}` mapping, deliberately: `configs/datasets.yaml` ships to db-api as a
  ConfigMap (`deploy.sh`) independently of the db-api image, so any shape change to
  `columns` reaches a running pod whose code predates it. db-api's
  `load_column_descriptions` would pass the mapping straight into `/schema`'s
  `description` field, and the browser's schema drawer renders that string as a React
  child. An unknown sibling key is invisible to every existing reader instead.
- Types are **not** re-verified against live BigQuery at build time — the harness is
  offline by design. Re-derive them with the query in
  `docs/adding-datasets.md` whenever a view's SQL changes.

**Consumer**: `scripts/gen-sandbox-docs.py` (the sandbox schema markdown). db-api takes its
`/schema` types from the live BigQuery job schema and ignores this block.

### Field details for `tables.<table>.categorical_columns`

The value for each categorical column entry controls how distinct values are fetched:

| Value  | Meaning |
|--------|---------|
| `null` | Independent -- all distinct values returned as a flat list |
| `"resource"` | Dependent -- distinct values grouped by the named parent column |

Example: `dataset: "resource"` means the allowed dataset values differ per resource.

**Consumer**: db-api (`_TABLE_DESCRIPTIONS`, `_COLUMN_DESCRIPTIONS`, `_TABLE_EXAMPLES`, `_CATEGORICAL_COLUMNS`)

## Section: `dataset_to_resource_rules`

Ordered list of pattern-based rules mapping the `dataset` column values in BigQuery
tables to `resource` identifiers. Used to generate SQL `CASE/WHEN` expressions in
BigQuery views, and by the results-api for Python-side mapping.

Rules are evaluated top-to-bottom; the first matching rule wins. A fallback rule
with `pattern: "*"` should be last.

```yaml
dataset_to_resource_rules:
  - pattern: string        # required -- SQL LIKE pattern (% for wildcard) or "*" for fallback
    resource: string|null  # required -- resource_id to assign, or null when using transform
    transform: string      # optional -- transformation to apply (only "lowercase" supported)
    comment: string        # optional -- explains why the rule exists
```

### Pattern semantics

| Pattern | SQL equivalent | Matches |
|---------|---------------|---------|
| `"FinnGen%MVP_UKBB%"` | `dataset LIKE 'FinnGen%MVP_UKBB%'` | FinnGen_R13_MVP_UKBB, FinnGen_R13_MVP_UKBB_labs |
| `"FinnGen%UKBB%"` | `dataset LIKE 'FinnGen%UKBB%'` | FinnGen_R13_UKBB, FinnGen_R13_UKBB_labs |
| `"FinnGen%"` | `dataset LIKE 'FinnGen%'` | All other FinnGen datasets |
| `"genebass"` | `dataset = 'genebass'` | Exact match |
| `"*"` + `transform: "lowercase"` | `ELSE LOWER(dataset)` | Fallback: lowercased dataset name becomes resource |

**Order matters**: more specific patterns must come before broader ones (e.g.
`FinnGen%MVP_UKBB%` before `FinnGen%`).

Different views may use different subsets of these rules. The `applies_to` field
(optional) restricts a rule to specific views:

```yaml
  - pattern: "genebass"
    resource: "genebass"
    applies_to: ["exome_variant_results_v", "gene_burden_results_v"]
```

When `applies_to` is omitted, the rule applies to all views that use resource mapping.

### Design note: version is not part of mapping rules

The mapping rules resolve dataset→resource only. The API's Python-side consumer also
needs a `(resource, version)` tuple, but version is always resolved from the profile's
dataset registry (`profiles.<profile>.datasets.<id>.version`), not from the mapping
rules. This keeps the rules focused on SQL view generation and resource lookup.

**Consumer**: db SQL view generation script, results-api dataset-to-resource mapper

## Section: `profiles`

Per-deployment-profile configuration. Each profile has its own dataset registry and
GCS bucket paths. The two current profiles are `finngen` (internal FinnGen access) and
`daly` (Broad-hosted copy with different bucket paths).

```yaml
profiles:
  <profile_name>:
    datasets:
      <dataset_id>:              # stable identifier referenced by product configs
        resource: string         # required -- resource_id (must exist in `resources`)
        version: string          # required -- human-readable version label
        description: string      # required -- dataset description surfaced to API users
        author: string           # required -- study author / consortium
        publication_date: string # required -- YYYY-MM-DD or "NA"
        data_type: string        # required -- see enum below
        trait_type: string|null  # required -- see enum below; null for non-association data

        # optional fields
        metadata_file: string|null       # GCS path to per-phenotype metadata (null if none)
        metadata_harmonizer: string|null # harmonizer type name, or null
        n_samples: integer               # total sample size
        n_cases: integer                 # case count (binary traits)
        n_controls: integer              # control count (binary traits)
        n_phenotypes: integer            # number of phenotypes
        pseudo_credible_sets: boolean    # true if credible sets are pseudo (not fine-mapped)
        collection: boolean              # true if this is a collection of sub-studies
        subdataset_id_field: string      # field identifying sub-studies (when collection=true)
        qtl_types: [string]              # QTL types in collection (e.g. ["eQTL", "sQTL"])
        phenotypes:                      # inline phenotype list (small fixed-phenotype datasets)
          - phenotype_code: string
            phenotype_string: string
            n_cases: integer
            n_controls: integer
            n_samples: integer
```

### `data_type` enum

| Value | Description |
|-------|-------------|
| `gwas` | Genome-wide association study |
| `eqtl` | Expression QTL |
| `pqtl` | Protein QTL |
| `sqtl` | Splicing QTL |
| `caqtl` | Chromatin accessibility QTL |
| `asmqtl` | Allele-specific methylation QTL |
| `metaboqtl` | Metabolomics QTL |
| `mixed` | Multiple QTL types (collections) |
| `exome` | Exome variant-level results |
| `gene_based` | Gene-level burden test results |
| `expression` | Gene/protein expression levels |
| `chromatin_peaks` | Chromatin accessibility peaks |
| `open_chromatin` | Atlas of accessible/active chromatin regions by cell type/tissue |
| `variant_effect` | In-silico predicted variant effect on chromatin (e.g. ChromBPNet, FLARE) |
| `mpra` | Measured cis-regulatory allelic activity from a massively parallel reporter assay (MPRA) |
| `hla` | Classical HLA allele associations (association unit is an imputed HLA allele, not a variant) |
| `gene_disease` | Gene-disease associations |

Disambiguating the chromatin-related data types (note `caqtl` is a measured QTL with `trait_type: quantitative`; the others — `chromatin_peaks`, `open_chromatin`, `variant_effect`, `mpra` — carry `trait_type: null`):

- `caqtl` -> the accessibility QTL (a measured variant-accessibility association).
- `chromatin_peaks` -> the single-cell peak-to-gene link product.
- `open_chromatin` -> an atlas of accessible/active regions; any gene links are secondary
  (via `target_gene`).
- `variant_effect` -> in-silico predicted effect of a variant on accessibility, not a
  measured QTL. Usually per cell-type context (some scores, e.g. FLARE, are pan-context
  with `cell_type` null).
- `mpra` -> *measured* cis-regulatory allelic activity from a reporter assay (emVar /
  active / log2Skew), per cell line plus a cross-line `meta` call. Distinct from
  `variant_effect` (in-silico prediction) and from `caqtl`/eQTL: MPRA reads out intrinsic
  reporter activity out of native chromatin context, not an endogenous QTL association.

`hla` is an association data type like `gwas` (it carries a real `trait_type`), but its
unit is an imputed classical HLA **allele** rather than a nucleotide variant, so its rows
carry `gene`/`allele` instead of `ref`/`alt` and never join to variant-keyed data on
chr/pos/ref/alt. Keep it separate from `gwas` for exactly that reason: a consumer that
assumes a variant key would silently mis-handle it.

### `trait_type` enum

| Value | Description |
|-------|-------------|
| `binary` | Case/control phenotypes |
| `quantitative` | Continuous phenotypes |
| `mixed` | Both binary and quantitative |
| `null` | Non-association data (expression, chromatin_peaks, open_chromatin, variant_effect, mpra, gene_disease) |

### Profile differences

The finngen and daly profiles share identical dataset definitions and descriptions.
They differ only in `metadata_file` GCS paths, which point to different buckets:

- **finngen**: `gs://finngen-commons/results_api_data/...`
- **daly**: `gs://daly-genetics-results/...`

When `metadata_file` is `null`, the entry is identical across profiles.

**Consumer**: results-api (`datasets` registry, dataset-to-resource mapping)

## How each service consumes the config

### genetics-results-api (results-api)

Reads `profiles.<active_profile>.datasets` to build:
1. The **dataset registry** dict keyed by `dataset_id` -- used for the `/datasets` endpoint
   and by product configs (credible_sets, coloc, summary_stats) that reference datasets
2. The **dataset-to-resource mapping** -- currently in `common.py` as `dataset_to_resource`,
   used to group datasets under resources for the API

The active profile is selected via `CONFIG_PROFILE` env var.

### genetics-results-db (db-api)

Reads profile-independent sections:
1. `resources` -> builds `_RESOURCE_METADATA` dict
2. `tables` -> builds `_TABLE_DESCRIPTIONS`, `_COLUMN_DESCRIPTIONS`, `_TABLE_EXAMPLES`, `_CATEGORICAL_COLUMNS`

Does not currently use the per-profile datasets section directly.

### SQL view generation (build-time)

A generation script reads `dataset_to_resource_rules` to produce `CASE/WHEN` SQL
fragments used in BigQuery view definitions (`schemas/*.sql`).

## Relationship between resources and datasets

```
resource (1) <------- (*) dataset
   |                       |
   |  shared metadata:     |  per-profile:
   |  - label              |  - version
   |  - description        |  - description
   |  - aliases            |  - metadata_file (GCS path)
   |                       |  - author, publication_date, etc.
   |
   +-- used in BQ views as the `resource` column
   +-- derived from `dataset` column via mapping rules
```

A **resource** is a data source (e.g. "finngen", "ukbb"). Multiple **datasets** can
belong to the same resource (e.g. `finngen_gwas`, `finngen_pqtl`, `finngen_eqtl` all
map to resource `finngen`). The `dataset_to_resource_rules` define how raw dataset
column values in BigQuery are mapped to resource identifiers in SQL views.

### Resource ID reconciliation note

Some BigQuery tables use versioned resource IDs (e.g. `bipex2`, `schema2`) while the
API dataset registry references unversioned IDs (e.g. `bipex`, `schema`). When
populating the real `datasets.yaml`, these must be reconciled so that mapping rules,
resource definitions, and dataset entries all use consistent identifiers.

## Dev environment

**"Dev environment" here means running the service processes on a workstation, and
nothing more.** There is no dev namespace and no dev BigQuery dataset for either
production brand: `phewas-development` *is* production, and so is the daly `finngenie`
cluster. The suite runs three clusters in two projects (`docs/environments.md`) — the
`daly-staging` cluster is a rehearsal ground for **manifests and images only**, since its
db-api points at the same `daly-finngenie:genetics_results` that daly production reads
(`genetics-results-suite-zaw`). A locally-run service therefore reads a **live**
`genetics_results` unless it is pointed elsewhere. To rehearse a BigQuery schema or view
change against a throwaway dataset instead, see `docs/bigquery-dev-dataset.md` and
`scripts/bq-dev-dataset.sh`.

Each service repo (genetics-results-api, genetics-results-db) runs independently in
its own tmux window during local development. To keep them in sync with the canonical
`configs/datasets.yaml` from the suite repo:

**Syncing the config**

From the suite repo root, run:

```bash
./scripts/sync-datasets.sh
```

This copies `configs/datasets.yaml` to `../genetics-results-db/configs/datasets.yaml`
and `../genetics-results-api/configs/datasets.yaml`, creating the `configs/` directories
if needed.

**How services load the config**

Each service reads datasets.yaml from the path in the `DATASETS_CONFIG_PATH` env var,
defaulting to `./configs/datasets.yaml` when unset.

**Gitignored local copy**

The local copy of datasets.yaml is gitignored in both `genetics-results-api` and
`genetics-results-db` and is never committed. `scripts/sync-datasets.sh` (or
deploy.sh calling it) places it for local dev only; it is never baked into a
deployed image. At runtime the ConfigMap generated from this repo's canonical
`configs/datasets.yaml` is authoritative, so a stale or missing local copy only
affects local dev. Committing the copies so CI could compare them against the
canonical version was once written down as the intent, but was never
implemented and would first require these repos to have CI at all.
