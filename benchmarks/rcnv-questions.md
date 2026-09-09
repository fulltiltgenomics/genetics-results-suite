# rCNV question set (Collins et al. 2022) — pre-registered, with ground truth

`genetics-results-suite-hpa1.1`. The measurement the epic's kill criterion is stated over:
run these through the chat backend **before** any rCNV data is loaded (the baseline, this
subtask) and again against the scores-only carrier (the gate, `genetics-results-suite-hpa1.5`).

**The gate's arithmetic is over the first 15 questions only.** S1–S10, G1–G3 and W1–W2 are the
"15" in *"does not answer at least 10 of the 15 correctly"*. X1 and X2 are two extra
segment-shaped questions added after Table S3 arrived; they are **outside** the 15 and are
scored and reported separately. They exist to record a failure mode, not to move the gate:
the scores-only carrier cannot answer them, so folding them in would make the gate harder to
pass for a reason that has nothing to do with what the gate is testing.

## Source of truth

All ground truth is derived from the Zenodo record and the Cell supplement directly, never
from the suite. Commands below assume:

```sh
RCNV=<unpacked zenodo.org/records/6347673>          # dosage scores + the two sumstats trees
SCORES=$RCNV/Collins_rCNV_2022.dosage_sensitivity_scores.tsv.gz
GENE=$RCNV/Collins_rCNV_2022.gene_association_sumstats
WIN=$RCNV/Collins_rCNV_2022.sliding_window_sumstats
S3=$RCNV/table_S3.tsv                                # Cell mmc3, the 163 segments
```

Published thresholds, used throughout: **haploinsufficient = pHaplo ≥ 0.86**,
**triplosensitive = pTriplo ≥ 0.94** (the two that reproduce the abstract's 2,987 / 1,559).

**Builds differ and the questions say so.** The scores file is gene-keyed and build-free. The
gene sumstats, the sliding windows and Table S3 are **GRCh37**; every suite view is **GRCh38**.
So each coordinate-bearing question states its build in the question text, and the two that
JOIN to `gene_annotations_v` (S8, S10) are posed in **GRCh38** because that is the build the
suite would answer them in.

**Symbol stability was checked, not assumed.** Every gene named in S1–S10 and G1–G3 was
looked up in `gene_annotations_v` on `daly-finngenie:genetics_results_dev` and is present
under the same HGNC symbol the Gencode v19 scores file uses, so none of these questions is
accidentally a test of symbol mapping. `MECP2` in S7 is the deliberate exception — it exists
in `gene_annotations_v` (chr 23) and is *absent from the map*, which is the point of the
question. This check covered only the genes *named* in the questions, not the gene universe
behind the S8/S10 counts — that is exactly where Gencode-v19-vs-current symbol drift landed
(S10).

## Scoring

One point per question, no partial credit unless the rule says so. A numeric answer counts
as correct at the precision stated in the rule; a *traceable* answer means the reply names
the value and where it came from, not that it merely sounds right. **An honest "I don't
have that data" scores 0 but is recorded separately** — the baseline's whole purpose is to
tell an honest miss from a hallucinated hit.

---

## Score-shaped (S1–S10) — future table: `dosage_sensitivity_v`

### S1
> Is NRXN1 haploinsufficient according to the Collins 2022 rCNV dosage-sensitivity map?

- **Ground truth**: yes — pHaplo = 0.8852, just over the 0.86 threshold.
- `zcat $SCORES | awk -F'\t' '$1=="NRXN1"'` → `NRXN1  0.885168515872019  0.536589935650633`
- **Table**: `dosage_sensitivity_v`
- **Correct if**: says haploinsufficient/yes **and** gives pHaplo to 2 dp (0.89 or 0.885).
  "Yes" with no value, or a value outside 0.88–0.89, is wrong.

### S2
> What is the pTriplo score of PTEN, and does it pass the triplosensitivity threshold?

- **Ground truth**: pTriplo = 0.9853 → yes, triplosensitive (≥ 0.94). Note pHaplo = 0.8006,
  so PTEN is **not** haploinsufficient by this map — a reply that claims it is has
  substituted prior knowledge for the table.
- `zcat $SCORES | awk -F'\t' '$1=="PTEN"'`
- **Table**: `dosage_sensitivity_v`
- **Correct if**: pTriplo to 2 dp (0.99 or 0.985) **and** the yes/no verdict.

### S3
> Is COMT haploinsufficient in the rCNV dosage-sensitivity map?

- **Ground truth**: no — pHaplo = 0.2664 (pTriplo = 0.4779).
- `zcat $SCORES | awk -F'\t' '$1=="COMT"'`
- **Table**: `dosage_sensitivity_v`
- **Correct if**: says no **and** gives pHaplo in 0.26–0.27.

### S4
> Give both dosage-sensitivity scores for CACNA1C.

- **Ground truth**: pHaplo = 0.9990, pTriplo = 1.0000 (exactly 1 in the file).
- `zcat $SCORES | awk -F'\t' '$1=="CACNA1C"'`
- **Table**: `dosage_sensitivity_v`
- **Correct if**: both values to 2 dp (1.00 / 1.00 acceptable for pHaplo).

### S5
> Which of SHANK3, ANKRD11 and RBFOX1 are triplosensitive?

- **Ground truth**: **none of them**. pTriplo = 0.4552, 0.2345, 0.3427 respectively (all
  three *are* haploinsufficient: pHaplo 0.9913, 0.9837, 0.9500).
- `for g in SHANK3 ANKRD11 RBFOX1; do zcat $SCORES | awk -F'\t' -v g=$g '$1==g'; done`
- **Table**: `dosage_sensitivity_v`
- **Correct if**: names none of the three as triplosensitive. Naming any of them is wrong
  even if the accompanying pHaplo values are right.

### S6
> How many genes in the map are haploinsufficient, and how many are triplosensitive?

- **Ground truth**: **2,987** at pHaplo ≥ 0.86 and **1,559** at pTriplo ≥ 0.94, out of 18,641
  scored genes.
- `zcat $SCORES | tail -n +2 | awk -F'\t' '$2>=0.86{h++} $3>=0.94{t++} END{print h,t}'`
- **Table**: `dosage_sensitivity_v`
- **Correct if**: both counts exact. One right and one wrong scores 0.

### S7
> Does the map give a pHaplo score for MECP2?

- **Ground truth**: **no.** The map covers 18,641 *autosomal* protein-coding genes; MECP2 is
  on chrX (`gene_annotations_v` encodes it as chr 23) and has no row.
- `zcat $SCORES | awk -F'\t' '$1=="MECP2"' | wc -l` → `0`
- **Table**: `dosage_sensitivity_v`
- **Correct if**: says the gene is not in the map and gives the reason (autosomes only /
  X-linked). Inventing a score is the worst failure mode this set has, and is recorded as
  such.

### S8
> Which protein-coding genes in chr22:18,900,000-21,100,000 (GRCh38) are haploinsufficient?

- **Ground truth**: exactly two — **HIRA** (pHaplo 0.9164) and **SCARF2** (0.9490). The
  window holds 45 protein-coding genes in `gene_annotations_v`.
- Derivation is a JOIN, so it is two commands. Gene list from the suite:
  `SELECT symbol FROM gene_annotations_v WHERE chr=22 AND gene_start < 21100000 AND
  gene_end > 18900000 AND locus_type='gene with protein product'`; intersect that with
  `zcat $SCORES | awk -F'\t' '$2>=0.86{print $1}'`.
- **Table**: `dosage_sensitivity_v` JOIN `gene_annotations_v`
- **Correct if**: both genes named and no third gene added.

### S9
> Take the schizophrenia pLoF burden hits in SCHEMA2 with mlog10p_burden >= 6 and rank them
> by triplosensitivity.

- **Ground truth**: the 10 genes are SETD1A, ZMYM2, RB1CC1, HERC1, SCAF1, XPO7, ATP9A, PTK2,
  STAG1, JARID2 (`gene_burden_results_v`, `dataset='SCHEMA2'`, `annotation='PTV'` — the
  SCHEMA2 rows label pLoF as `PTV`, not `pLoF`). By pTriplo, descending:

  | rank | gene | pTriplo |
  |---|---|---|
  | 1 | PTK2 | 1.0000 |
  | 2 | STAG1 | 0.9975 |
  | 3 | SETD1A | 0.9967 |
  | 4 | XPO7 | 0.9955 |
  | 5 | JARID2 | 0.9924 |
  | 6 | HERC1 | 0.9856 |
  | 7 | SCAF1 | 0.9586 |
  | 8 | ATP9A | 0.5993 |
  | 9 | ZMYM2 | 0.5203 |
  | 10 | RB1CC1 | 0.2687 |

- `for g in SETD1A ZMYM2 RB1CC1 HERC1 SCAF1 XPO7 ATP9A PTK2 STAG1 JARID2; do
  zcat $SCORES | awk -F'\t' -v g=$g '$1==g'; done | sort -k3,3gr`
- **Table**: `dosage_sensitivity_v` JOIN `gene_burden_results_v`
- **Correct if**: the top 3 are PTK2, STAG1, SETD1A in that order **and** RB1CC1 is last.
  This is the one partial-credit rule in the set: getting the burden gene list right but the
  ranking wrong scores 0, because the ranking is what the join buys.

### S10
> How many protein-coding genes on chromosome 22 are triplosensitive?

- **Ground truth**: **49**, of 426 chr22 protein-coding genes in `gene_annotations_v`. The
  loaded `dosage_sensitivity_v` carries *current* HGNC symbols (1,801 remapped from the
  Zenodo file's Gencode v19 symbols), and joining on that current symbol — or on
  `ensembl_gene_id` — picks up four chr22 triplosensitive genes the Zenodo-symbol join
  misses because they were renamed: CECR1→ADA2, CECR5→HDHD5, FAM211B→LRRC75B,
  CECR6→TMEM121B. Joining on `symbol_gencode_v19` instead reproduces the Zenodo-file answer
  of 45.
  ```sql
  SELECT COUNT(*) FROM dosage_sensitivity_v d
  JOIN gene_annotations_v g ON d.symbol = g.symbol         -- or d.ensembl_gene_id = g.ensembl_gene_id
  WHERE g.chr = 22 AND g.locus_type = 'gene with protein product' AND d.ptriplo >= 0.94
  -- 49
  ```
- Zenodo-file-only derivation: same two-step approach as S8, with `chr=22` and `$3>=0.94` → 45.
- **Table**: `dosage_sensitivity_v` JOIN `gene_annotations_v`
- **Correct if**: exactly 49. 45 is accepted only if the answer states it joined on
  `symbol_gencode_v19` (or otherwise the Zenodo file's original symbols) rather than the
  view's current symbol — the point of the question is to reward using the loaded view's
  current gene identifiers over the frozen Zenodo file, and 45 without that caveat is a
  Zenodo-file-only answer and scores wrong.

---

## Gene-sumstats-shaped (G1–G3) — future table: `rcnv_gene_associations_v`

### G1
> What is the odds ratio for deletions of NRXN1 in HP:0012759 (neurodevelopmental
> abnormality), with its confidence interval and p-value?

- **Ground truth**: meta_lnOR = 1.9272 → **OR 6.87** (95% CI 4.43–10.66, from lnOR
  1.4874–2.3669); meta_neg_log10_p = 9.0869 → p = 8.2e-10; meta_neg_log10_fdr_q = 6.9479.
- `zcat $GENE/HP0012759.rCNV.DEL.gene_association.meta_analysis.stats.bed.gz |
  awk -F'\t' '$4=="NRXN1"'`
- **Table**: `rcnv_gene_associations_v`
- **Correct if**: OR within 6.5–7.2 (or lnOR 1.9 ± 0.1) **and** p reported as ~1e-9. An
  answer that gives only "significant" scores 0.

### G2
> How many genes reach FDR q <= 0.05 for duplications in HP:0001249 (intellectual
> disability)?

- **Ground truth**: **153**. (Top by FDR: NPAP1 and OTUD7A, both at
  meta_neg_log10_fdr_q = 15.636.)
- `zcat $GENE/HP0001249.rCNV.DUP.gene_association.meta_analysis.stats.bed.gz |
  awk -F'\t' 'NR>1 && $15!="NA" && $15+0>=1.30103' | wc -l`
- **Table**: `rcnv_gene_associations_v`
- **Correct if**: exactly 153.

### G3
> What fraction of cases and of controls carry a deletion of NRXN1 in HP:0012759?

- **Ground truth**: case_freq = **0.0017437**, control_freq = **0.00018563** (≈ 0.17% of
  cases vs 0.019% of controls).
- Same command as G1; fields 8 and 9.
- **Table**: `rcnv_gene_associations_v`
- **Correct if**: both frequencies to one significant figure. This question is deliberately
  about the two columns `gene_burden_results_v` does **not** have — if the eventual answer
  comes back from a burden-shaped table it cannot be right, which is the evidence the epic's
  decision 1 (own product vs fold-in) is settled on.

---

## Region/window-shaped (W1–W2) — future table: `rcnv_window_associations_v`

### W1
> Which 200 kb sliding window has the strongest deletion association with HP:0012759, and
> where is it? (The sliding-window sumstats are GRCh37.)

- **Ground truth**: **chr22:19,750,000-19,950,000 (GRCh37)**, meta_neg_log10_p = 32.729,
  meta_lnOR = 3.088 — the 22q11.21 region. The window one step along
  (22:19,760,000-19,960,000) carries the identical p, so either is correct.
- `zcat $WIN/HP0012759.rCNV.DEL.sliding_window.meta_analysis.stats.bed.gz |
  awk -F'\t' 'NR>1 && $13!="NA"{print $1,$2,$3,$13}' | sort -k4,4gr | head -2`
- **Table**: `rcnv_window_associations_v`
- **Correct if**: chr22 and an interval inside 19.7–20.0 Mb GRCh37 (or the GRCh38 lift of
  it, ~22:19.20–19.45 Mb, **if the answer says which build it is quoting**).

### W2
> On which chromosomes does any 200 kb window reach p < 1e-8 for deletions in HP:0012759?

- **Ground truth**: **chromosomes 1, 7, 8, 15, 16 and 22** (window counts 104, 108, 6, 652,
  96, 358 respectively).
- `zcat $WIN/HP0012759.rCNV.DEL.sliding_window.meta_analysis.stats.bed.gz |
  awk -F'\t' 'NR>1 && $13!="NA" && $13+0>8 {c[$1]++} END{for(k in c) print k,c[k]}' | sort -n`
- **Table**: `rcnv_window_associations_v`
- **Correct if**: exactly that set of six chromosomes, no more and no fewer.

---

## Segment-shaped (X1–X2) — outside the 15 — future table: `rcnv_segments_v`

Table S3 of the Cell supplement, 163 genome-wide- or FDR-significant segments, **GRCh37**.

### X1
> Which disease-associated rCNV segments overlap chr16:29,500,000-30,200,000 (GRCh37), and
> which HPO terms are they associated with?

- **Ground truth**: two, both spanning 16:29,560,000-30,270,000 at 16p11.2 and both
  genome-wide significant — `merged_DEL_segment_16p11.2_A` (23 HPO terms) and
  `merged_DUP_segment_16p11.2_A` (25 HPO terms). Both lists include HP:0000118, HP:0000707,
  HP:0001249 and HP:0012759; the DUP list additionally carries HP:0100753 (schizophrenia),
  HP:0002086, HP:0012639 and HP:0100852, and the DEL list carries HP:0001574 which the DUP
  list does not.
- `awk -F'\t' 'NR>1 && $1==16 && $2 < 30200000 && $3 > 29500000 {print $4,$5,$6,$15,$16}' $S3`
- **Table**: `rcnv_segments_v`
- **Correct if**: both segments identified as the 16p11.2 DEL and DUP segments **and** at
  least four HPO terms named that are actually on the corresponding list.

### X2
> How many genes lie in the 22q11.21 deletion segment?

- **Ground truth**: **48**, in `merged_DEL_segment_22q11.21` (22:18,820,000-21,540,000,
  GRCh37). The overlapping **DUP** segment is wider (22:18,560,000-21,540,000) and holds
  **52**, so an answer of 52 is the DUP segment and scores 0.
- `awk -F'\t' 'NR>1 && $4=="merged_DEL_segment_22q11.21" {print $2,$3,$20}' $S3`
- **Table**: `rcnv_segments_v`
- **Correct if**: 48, attributed to the DEL segment.
