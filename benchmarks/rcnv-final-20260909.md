# rCNV question set — final measurement, complete carrier (2026-09-09)

## DECISION: nocode 15/15, code 15/15 — segments nocode 2/2, code 2/2

**S1–S10 subtotal (the carrier-comparable slice, against the gate's nocode 5 / code 10):
nocode 10/10, code 10/10.** The gate set is S1–S10, G1–G3, W1–W2; X1–X2 are scored
separately and are outside the 15. Both arms answer every question in the set, and **no
value in either arm is fabricated** — every number traced to a row in a loaded view.

This is the epic's final measurement and the acceptance re-run for the db-api `/schema`
`BOOL` fix. The kill criterion ("at least 10 of the 15") was met by the code arm alone at
the gate; it is now met by both arms with five points to spare, and the nocode/code gap the
gate measured (5 points, all of it discovery) is **zero**.

Full transcripts, every tool call with its SQL or arguments, in
`benchmarks/rcnv-final-20260909.json`.

| run | nocode gate/15 | nocode S1–S10 | nocode seg/2 | code gate/15 | code S1–S10 | code seg/2 |
|---|---|---|---|---|---|---|
| baseline (no rCNV data) | 4 | 4 | 1 | 3 | 3 | 0 |
| gate (scores-only carrier) | 5 | 5 | 0 | 10 | 10 | 0 |
| **final (complete carrier)** | **15** | **10** | **2** | **15** | **10** | **2** |

## Precheck

Verified before any turn was spent, read-only:

```sh
POST /query {"sql":"SELECT COUNT(*) FROM rcnv_gene_associations_v"}   # 1,864,404
GET  /schema?table=dosage_sensitivity_v                              # 200
GET  /schema                                                         # 19 tables, warnings: []
kubectl -n genetics get pods -o custom-columns=…                     # chat-backend :20260909.14b2aeb
                                                                     # sandbox      :20260909.262bc8a
                                                                     # db-api       :20260909.9534903
```

`/schema` lists all four rCNV views (`dosage_sensitivity_v`, `rcnv_gene_associations_v`,
`rcnv_segments_v`, `rcnv_window_associations_v`) and the two `BOOL` categorical columns come
back as strings — `haploinsufficient.allowed_values = ["false","true"]`, likewise
`triplosensitive`. That is the hpa1.16 fix observed live: at the gate the same call returned
15 tables with `dosage_sensitivity_v` demoted into `warnings` as
`AttributeError: 'bool' object has no attribute 'lower'`, and `?table=` returned 500.

Run: `benchmarks/rcnv_runner.py`, one turn per question, no follow-ups, unique `session_id`
per question-and-arm, `secret: true`, model `claude-fable-5-1` (chat-backend `DEFAULT_MODEL`,
no override). Arms back to back on one machine behind a restarting port-forward; rate limits
lifted (2000/h) so no pacing was needed. **No turn was re-issued and no turn errored.**

## Per-question results

Verdicts are the pre-registered rules from `rcnv-questions.md`, applied as written. Neither
arm has a miss, so the "failure mode" column records how the answer was reached instead —
what would have been the failure mode is stated for the two questions where a wrong-but-
plausible answer was available (S10's 45, X2's 52/49).

### nocode arm (`tool_profile: nocode`, identity `mcp-tool`) — 15/15, S1–S10 10/10, segments 2/2

| # | v | tools / SQL or args | outcome |
|---|---|---|---|
| S1 | **1** | `get_dosage_sensitivity{genes:[NRXN1]}` | yes, pHaplo **0.885**, pTriplo 0.537 |
| S2 | **1** | `get_dosage_sensitivity{genes:[PTEN]}` | pTriplo **0.985**, triplosensitive yes; flagged pHaplo 0.801 as *below* the HI cutoff — the prior-knowledge trap avoided |
| S3 | **1** | `get_dosage_sensitivity{genes:[COMT]}` | no, pHaplo **0.266** |
| S4 | **1** | `get_dosage_sensitivity{genes:[CACNA1C]}` | pHaplo **0.999**, pTriplo **1.000** |
| S5 | **1** | `get_dosage_sensitivity{genes:[SHANK3,ANKRD11,RBFOX1]}` | "none of the three", with all three pTriplo values (0.455 / 0.234 / 0.343) — the gate's nocode pass here was ClinGen recollection; this one is the table |
| S6 | **1** | `get_database_schema{}`, `{table:rcnv_dosage_sensitivity_v}` (miss), `{table:dosage_sensitivity_v}`, then `query_database`: `COUNT(*), COUNTIF(haploinsufficient), COUNTIF(triplosensitive) FROM dosage_sensitivity_v` | **2,987 / 1,559** of 18,641, from the table rather than the abstract |
| S7 | **1** | `get_dosage_sensitivity{genes:[MECP2]}` | no — `count: 0`, reason given (map is autosomal only, MECP2 is Xq28) |
| S8 | **1** | `get_genes_in_region{chr:22,18.9–21.1 Mb,protein_coding}` then `get_dosage_sensitivity` on the 50 returned genes | **SCARF2 (0.949) and HIRA (0.916)**, no third gene; TBX1 0.850 named as sub-threshold rather than rounded in |
| S9 | **1** | `get_database_schema{table:gene_burden_results_v}`, `query_database`: `… WHERE resource='schema2' AND annotation='PTV' AND mlog10p_burden>=6`, then `get_dosage_sensitivity` on the 10 genes | **PTK2, STAG1, SETD1A** top three, **RB1CC1** last |
| S10 | **1** | `get_database_schema` ×3, `query_database` ×2: `dosage_sensitivity_v d JOIN gene_annotations_v a USING (ensembl_gene_id) WHERE a.chr=22 AND a.locus_type='gene with protein product' AND d.triplosensitive` | **49**, joined on the view's current identifiers. The available wrong answer (45, the Zenodo-symbol join) was not taken |
| G1 | **1** | `get_rcnv_associations{gene:NRXN1, phenotype:"HP:0012759", cnv_type:DEL}` | **OR 6.87 (4.42–10.67)**, **p 8.2e-10**, FDR q 1.1e-7 |
| G2 | **1** | `get_rcnv_associations{phenotype:"HP:0001249", cnv_type:DUP, max_fdr_q:0.05, limit:5000}` | **153**, from the tool's `count`; top hits OTUD7A/NPAP1 as expected |
| G3 | **1** | `get_rcnv_associations{gene:NRXN1, phenotype:"HP:0012759", cnv_type:DEL}` | cases **0.174 %**, controls **0.0186 %** — the two columns `gene_burden_results_v` does not have |
| W1 | **1** | `get_database_schema{table:rcnv_window_associations_v}`, `query_database`: `… WHERE phenotype='HP0012759' AND cnv_type='DEL' ORDER BY mlog10p DESC`, then `get_genes_in_region` on the winner | **chr22:19,760,000–19,960,000 (GRCh37)**, mlog10p 32.73, GRCh38 lift quoted alongside and labelled; called the tie with the neighbouring window explicitly |
| W2 | **1** | `get_database_schema`, `query_database`: `… AND mlog10p>8 GROUP BY chr` | **chr 1, 7, 8, 15, 16, 22** — exactly the six, no more, no fewer |
| X1 | **1** | `get_database_schema{table:rcnv_segments_v}`, `query_database`: `rcnv_segments_v CROSS JOIN UNNEST(associated_hpos) … WHERE chr=16 AND segment_start_grch37<=30200000 AND segment_end_grch37>=29500000` | both **16p11.2 DEL and DUP** segments, 23 / 25 HPO groups enumerated with names; DEL-only HP:0001574, DUP-only HP:0100753 both called out |
| X2 | **1** | `get_database_schema`, `query_database`: `… WHERE cytoband='22q11.21'` | **48**, attributed to `merged_DEL_segment_22q11.21` with its GRCh37 span; the DUP segment's 52 named as the reciprocal, not the answer |

### code arm (`tool_profile: code`, gateway-asserted identity) — 15/15, S1–S10 10/10, segments 2/2

Every turn is `run_analysis` calling `genetics.sql()`; 18 calls over 17 turns.

| # | v | SQL the turn issued | outcome |
|---|---|---|---|
| S1 | **1** | `… FROM dosage_sensitivity_v WHERE symbol='NRXN1'` | yes, pHaplo **0.885** |
| S2 | **1** | `… WHERE symbol='PTEN'` | pTriplo **0.985**, yes; pHaplo 0.801 flagged as below the HI cutoff |
| S3 | **1** | `… WHERE symbol='COMT' OR symbol_gencode_v19='COMT'` | no, pHaplo **0.266** |
| S4 | **1** | `… WHERE symbol='CACNA1C'` | **0.999 / 1.000** |
| S5 | **1** | `… WHERE symbol IN ('SHANK3','ANKRD11','RBFOX1') ORDER BY ptriplo DESC` | none of the three, all three pTriplo values |
| S6 | **1** | `COUNT(*), COUNTIF(haploinsufficient), COUNTIF(triplosensitive), COUNT(DISTINCT ensembl_gene_id) FROM dosage_sensitivity_v` | **2,987 / 1,559** of 18,641 |
| S7 | **1** | three keys (`symbol`, `symbol_gencode_v19`, `ensembl_gene_id='ENSG00000169057'`), then `gene_annotations_v` for the locus and a chr-23 count over the join | no — 0 rows on all three keys, chrX absent from the view entirely, reason given |
| S8 | **1** | `dosage_sensitivity_v d JOIN gene_annotations_v a USING (ensembl_gene_id) WHERE a.chr=22 AND a.locus_type='gene with protein product' AND a.gene_start<=21100000 AND a.gene_end>=18900000` | **SCARF2 and HIRA**, no third gene; TBX1 0.850 flagged sub-threshold |
| S9 | **1** | distinct-annotation probe, then `gene_burden_results_v b LEFT JOIN dosage_sensitivity_v d ON d.symbol=b.gene WHERE b.dataset='SCHEMA2' AND b.annotation IN ('PTV','pLoF') AND b.mlog10p_burden>=6 ORDER BY d.ptriplo DESC` | **PTK2, STAG1, SETD1A** top three; **RB1CC1** last |
| S10 | **1** | `COUNT(DISTINCT d.ensembl_gene_id) … JOIN gene_annotations_v USING (ensembl_gene_id) WHERE chr=22 AND locus_type='gene with protein product' AND d.triplosensitive` | **49** |
| G1 | **1** | `rcnv_gene_associations_v LEFT JOIN phenotypes_v … WHERE symbol='NRXN1' AND phenotype='HP0012759'`, computing `EXP(beta)` and `POW(10,-mlog10p)` in SQL | **OR 6.87 (4.42–10.67)**, **p 8.2e-10**; also reported the DUP row as tested-but-unestimated |
| G2 | **1** | `COUNT(DISTINCT ensembl_gene_id) … WHERE phenotype='HP0001249' AND cnv_type='DUP' AND beta IS NOT NULL AND mlog10_fdr_q >= -LOG10(0.05)`, plus three stricter variants | **153** as asked, with 142 / 112 / 106 offered as the paper's stricter tiers rather than substituted for the answer |
| G3 | **1** | same row, `case_freq` / `control_freq` | cases **0.00174**, controls **0.000186** |
| W1 | **1** | `rcnv_window_associations_v WHERE phenotype='HP0012759' AND cnv_type='DEL'`, ranked by `mlog10p` (2nd attempt used `QUALIFY ROW_NUMBER()`), then genes over the winning GRCh38 interval | **chr22:19,750,000–19,950,000 (GRCh37)**, mlog10p 32.73, GRCh38 lift labelled; tie with the adjacent window stated |
| W2 | **1** | `… AND mlog10p>8 GROUP BY chr`, then merged overlapping windows into loci in Python | **chr 1, 7, 8, 15, 16, 22**, each mapped to its recurrent locus |
| X1 | **1** | `rcnv_segments_v CROSS JOIN UNNEST(associated_hpos) LEFT JOIN phenotypes_v … WHERE chr=16 AND segment_start_grch37<30200000 AND segment_end_grch37>29500000` | both segments, 23 / 25 HPO groups with names, DEL-only and DUP-only sets separated |
| X2 | **1** | `… WHERE cytoband='22q11.21' OR segment_id LIKE '%22q11.21%'`, reading `n_genes` and `genes` | **48** for `merged_DEL_segment_22q11.21` with its GRCh37 span; DUP's 52 named as the reciprocal |

## What changed for the nocode arm, specifically

The gate's finding was that the nocode arm's 5/15 measured *a broken discovery path, not the
carrier*: `/schema` dropped `dosage_sensitivity_v` into a `warnings` array, so the view was
invisible to `get_database_schema`, and on eight of ten score-shaped turns the arm concluded
"none of my tools carry the Collins 2022 scores" and went to `web_search` /
`search_scientific_literature`. Three things are different now, and they are separable.

1. **`get_database_schema` lists the view.** The `/schema` `BOOL` crash is fixed: 19 tables,
   empty `warnings`, `?table=dosage_sensitivity_v` 200, and the two `BOOL` categorical
   columns render as `"false"`/`"true"`. Eleven `get_database_schema` calls across seven turns,
   **not one of which read a warnings array** — at the gate that array was the only place the
   view's name appeared, and it was reached by luck on two turns.
2. **The arm mostly does not need the schema call at all.** The two dedicated tools carry
   eight and three turns respectively: `get_dosage_sensitivity` answers S1–S5, S7, S8, S9's
   score half; `get_rcnv_associations` answers G1, G2, G3 outright, each in a single call.
   Where a question is a JOIN or an aggregate the arm falls back to
   `get_database_schema` → `query_database` (S6, S9, S10, W1, W2, X1, X2) and writes the SQL
   itself. So the split is *dedicated tool for lookups, live schema plus SQL for everything
   else* — which is what the tools were shaped for.
3. **Zero external-search calls in either arm, on any question.** The gate's nocode arm made
   roughly two dozen `web_search` / `search_scientific_literature` calls; this run made none.
   Every answer in this document came out of the database.

One discovery wrinkle worth recording, because it costs a turn when it bites: the loaded
phenotype key has **no colon** — `HP0012759`, not `HP:0012759`. `get_rcnv_associations`
normalises the colon form (G1/G2/G3 passed `"HP:0012759"` and got rows), but a `query_database`
turn must use the stored spelling, and each SQL turn got it right only after reading the
column's `allowed_values` from `get_database_schema`. S6 and S10 also each burned a
`get_database_schema{table: rcnv_dosage_sensitivity_v}` on a guessed name before finding
`dosage_sensitivity_v` — the un-prefixed name is the one exception to the `rcnv_*` family.

## The G/W/X questions, now answerable

At the gate these were ten honest "not available" turns (G1–G3, W1–W2 × 2 arms) plus 0/2 on
the segments; they were the evidence that the sumstats, window and segment increments were the
only route. All seven questions are now answered correctly in both arms, and the specific
things the gate said only the new tables could supply are exactly what the answers used:

- **G3** — the case/control carrier frequencies `gene_burden_results_v` structurally does not
  have (0.174 % vs 0.0186 %). This is the observation the epic's decision 1 (own rCNV product
  rather than a burden-table fold-in) was settled on, and it is now positive evidence rather
  than an absence.
- **G2** — 153 at FDR q ≤ 0.05. Both arms give the asked-for number *and* volunteer the
  paper's stricter tiers (code arm: 142 with the secondary-evidence gate, 112 at q < 0.01,
  106 with both) without substituting them for the answer.
- **W1/W2** — the sliding-window set answers both, and both arms **state which build they are
  quoting**, which the rule requires and which the gate's arms flagged as the reason they
  could not answer at all. W1 is reported in GRCh37 with the GRCh38 lift alongside; W2's six
  chromosomes are exact.
- **X1/X2** — the segments table closes the failure the gate diagnosed. X2 in particular: at
  the gate both arms answered 49 by counting `gene_annotations_v` protein-coding genes over a
  GRCh38 coordinate window, which the gate called "systematically the wrong answer, and wrong
  quietly". Both arms now read `n_genes` off `merged_DEL_segment_22q11.21`, answer **48**, and
  name the DUP segment's 52 as the reciprocal rather than the answer — precisely the behaviour
  the gate said the segments increment had to produce.

**Two loaded-data notes that do not change a verdict but should not rot silently:**

- **W2's window counts differ from the Zenodo file's** while the chromosome set does not.
  Ground truth counts windows 104 / 108 / 6 / 652 / 96 / 358 for chr 1 / 7 / 8 / 15 / 16 / 22;
  the loaded view gives 99 / 108 / 6 / 652 / 96 / 313. The deficit is on chr1 (−5) and chr22
  (−45) and is the GRCh38 liftOver dropping windows in pericentromeric and
  segmental-duplication regions. Both arms reported the loaded counts and both stated the
  unlifted fraction unprompted. The rule is over the *set*, so this is invisible to the score —
  it would not be if a question ever asked for a window count.
- **`merged_DEL_segment_22q11.21` had no GRCh38 lift in the load these runs queried**
  (`segment_start`/`segment_end` null); both arms noticed and said so. A GRCh38 coordinate
  query for that segment returned nothing, which is the safe failure, not a silent wrong one.
  The segments munge now composes that boundary from the sliding windows sitting on it, so
  the segment carries a GRCh38 pair; the hazard remains for the segments that still do not
  lift.

## Fabricated values

**None, in either arm, on any of the 34 turns.** Every quoted number was checked against the
question file's pre-registered ground truth or against the loaded view directly; the
independently re-derived spot checks (NRXN1 DEL row, the 153 count, the top window, the six
chromosomes, both 16p11.2 segments, both 22q11.21 gene counts) all match. The two answers
where a wrong number was readily available — S10's 45 and X2's 52 — were reached correctly
and the wrong alternative named as such.

## The three loose rules, re-checked

The gate reported what a tightened rule would give. Re-applied here, **nothing moves**:

1. **S5** tightened to require the three pTriplo values: both arms keep it (nocode 0.455 /
   0.234 / 0.343 from `get_dosage_sensitivity`; code the same from SQL). At the gate this
   tightening cost the nocode arm a point, because its pass was ClinGen recollection.
2. **S6** tightened to require the count come from the loaded table: both arms keep it, both
   ran `COUNTIF` over `dosage_sensitivity_v`. At the gate the nocode arm's 2,987 / 1,559 came
   from the abstract.
3. **X2** tightened to require the segment's GRCh37 coordinates alongside the count: both arms
   keep it; both quote 22:18,820,000–21,540,000.

**Tightened totals: nocode 15/15, code 15/15** — where the gate's tightened totals were
nocode 3/15, code 10/15.

## Delta versus the gate, per question

| # | nocode gate → final | code gate → final | what changed |
|---|---|---|---|
| S1–S4 | 0 → **+1** each | 1 → 1 | nocode: refusal → the dedicated tool. Code unchanged |
| S5–S7 | 1 → 1 | 1 → 1 | nocode's pass now comes from the table rather than ClinGen / the abstract / the file description |
| S8 | 0 → **+1** | 1 → 1 | nocode: ClinGen proxy (TBX1/LZTR1) → HIRA + SCARF2 from the scores |
| S9, S10 | 1 → 1 | 1 → 1 | unchanged; nocode reaches the view by name now instead of via the warnings array |
| G1–G3 | 0 → **+1** each | 0 → **+1** each | `rcnv_gene_associations_v` |
| W1, W2 | 0 → **+1** each | 0 → **+1** each | `rcnv_window_associations_v` |
| X1, X2 | 0 → **+1** each | 0 → **+1** each | `rcnv_segments_v`; X2's quiet-wrong 49 replaced by the segment's own 48 |
| **gate/15** | **5 → 15** | **10 → 15** | |
| **S1–S10** | **5 → 10** | **10 → 10** | the hpa1.16 comparison: the whole nocode deficit was discovery |
| **segments/2** | **0 → 2** | **0 → 2** | |

## Operational notes

- **Cost and time.** 34 scored turns, no re-issues, no truncated captures, no turn-level
  errors. Wall clock **580 s** on the nocode arm (11:15:09Z–11:24:49Z) and **496 s** on the
  code arm (11:24:49Z–11:33:05Z), **1,076 s ≈ 18 min end to end**. Tool calls: 32 nocode
  (8 `get_dosage_sensitivity`, 3 `get_rcnv_associations`, 11 `get_database_schema`,
  8 `query_database`, 2 `get_genes_in_region`), 18 code (all `run_analysis`). Non-cached input
  ~512 k tokens nocode / ~59 k code, output ~18 k / ~21 k. The nocode arm's much larger input
  is the 66-tool profile against the code profile's 20.
- **One in-turn script failure, self-corrected.** Code W1's first `run_analysis` raised
  `GeneticsError` (the stream carries no detail beyond the exception name); the second
  attempt replaced `ORDER BY … LIMIT 10` with `QUALIFY ROW_NUMBER()` over the 11.2 M-row
  window view and succeeded. It is the only non-`ok` script result in the run and it did not
  cost the turn.
- **Two identities, as the profiles require** — nocode as `mcp-tool` on the internal secret,
  code as the gateway-asserted operator address with `X-Gateway-Auth`. Identity enters no
  question. Rate limits were already lifted (2000/h), so the arms ran back to back with no
  pacing.
- **Nothing was mutated by this run.** `kubectl` use was `get`, `port-forward` and reading two
  secret values to sign requests; BigQuery access was `SELECT` only through db-api. Both arms
  sent `secret: true`, so staging's `chat_history.db` carries none of these turns.
