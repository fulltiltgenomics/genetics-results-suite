# deCODE pQTL (deCODE_pQTL_2021) in `credible_sets_v`: potential issues

Brief for whoever investigates the deCODE data. Everything below was measured on
`daly-finngenie.genetics_results` on 2026-10-08 with the queries given; nothing has been
changed or filed. The pipeline that produced the data is documented in
genetics-results-munge `docs/pseudo-credible-sets.md` ("The deCODE pQTL run") and
`scripts/munge_decode_pqtl.py`; the per-aptamer outputs are in
`gs://daly-genetics-results/credible_sets/decode_pseudo/seq.<id>.report.out.pseudo_cs.*.tsv`
(4,844 files). The source bucket (`finngen-commons`) is not readable from the daly VM, so
the checks that need the munged sumstats or the aligned source have to run on the finngen
side.

"Lead" below means the max-PIP row of each (trait, trait_original, cs_id).

## 1. Whole aptamers missing, including ones with a strong cis-pQTL on the same platform

FinnGen SomaScan (n = 1,000, SuSiE) has a cis credible set (±1 Mb of the coding gene) for
1,017 aptamers. 258 of those aptamers have **no row at all** in deCODE (n = 35,559), and
125 of the 258 carry ids in the SomaScan v4 range (all 4,808 deCODE aptamers have ids
below 20000, so this is the range to expect). Examples, with the FinnGen SomaScan cis
−log10 p: IL6R seq.4139.71 (183) and seq.8092.29 (180), PLXNB2 seq.10855.55 (182), AHSG
seq.10966.1 (182), PNLIPRP2 seq.6957.14 (138), SERPING1 seq.13710.6 (120), JAML seq.8232.90
(117), SAA1 seq.4336.2 (107), IL1RAP seq.2630.12 (96), C1S, SIGLEC5/14, GPNMB (both
aptamers), CCL15, CDH11, TNC. None of these has a per-aptamer pseudo-CS file in the bucket,
so they were never in the autoreporting input array — which `munge_decode_pqtl.py
--input-array` restricts to aptamers with at least one p ≤ 5e-8 row in the aligned file.

Checks: (a) are these ids in `deCODE_pQTLs_NatGen2021_aligned_p0.005.tsv.gz.probes`
(4,907 ids)? (b) if yes, do they have rows in the aligned file, and in the unaligned
sibling `deCODE_pQTLs_NatGen2021_p0.005.tsv.gz`? An aptamer like IL6R seq.4139.71 cannot
lack a genome-wide-significant row in 35,559 Icelanders if it reached 1e-183 in 1,000
Finns. If rows exist only in the unaligned file, the 2023 gnomAD alignment (7 % of rows
unplaced) dropped them. The datasets.yaml description says 4,808 of 4,907 aptamers have a
GWS pQTL; the 99 that do not should be a list that can be checked against these 125.

Regenerate the list:

```sql
WITH leads AS (
  SELECT resource, dataset, trait, trait_original, chr, pos, mlog10p
  FROM `daly-finngenie.genetics_results.credible_sets_v`
  WHERE data_type="pQTL" AND (resource="decode" OR (resource="finngen" AND dataset="FinnGen_SomaScan"))
  QUALIFY ROW_NUMBER() OVER (PARTITION BY resource, dataset, trait, trait_original, cell_type, cs_id ORDER BY pip DESC)=1),
g AS (SELECT symbol, chr gchr, gene_start, gene_end FROM `daly-finngenie.genetics_results.gene_annotations_v`),
soma_cis AS (
  SELECT l.trait, l.trait_original, MAX(l.mlog10p) m FROM leads l
  JOIN g ON g.symbol=l.trait AND l.chr=g.gchr AND l.pos BETWEEN g.gene_start-1000000 AND g.gene_end+1000000
  WHERE l.dataset="FinnGen_SomaScan" GROUP BY 1,2),
dec_any AS (SELECT DISTINCT trait_original FROM leads WHERE resource="decode")
SELECT s.trait, s.trait_original, ROUND(s.m,1) soma_cis_mlog10p
FROM soma_cis s LEFT JOIN dec_any d USING (trait_original)
WHERE d.trait_original IS NULL ORDER BY s.m DESC
```

## 2. 80 aptamers whose shard ran and produced no locus

Of the 4,844 per-aptamer pseudo-CS files, 80 are header-only (under 400 bytes; list with
`gcloud storage ls -l 'gs://daly-genetics-results/credible_sets/decode_pseudo/seq.*'`).
Every aptamer in the input array had a p ≤ 5e-8 row, so a shard with zero loci means
either every candidate lead was dropped by `--finngen-variants-only` (variant absent from
the FinnGen panel) or the shard's LD/grouping step failed without failing the task. First
ids: seq.10040.63, seq.10058.1, seq.10583.1, seq.11194.6, seq.11342.59, seq.11381.56,
seq.11544.39, seq.11590.5, seq.11626.7, seq.12424.107. For each, look at the strongest row
in its munged sumstat and whether that variant exists in the FinnGen R12 LD panel.

## 3. Cis signals absent from aptamers that do have a report (OAS1)

OAS1 seq.10361.25 (the aptamer Zhou et al. 2021 used; its cis-pQTL at the splice variant
12:112919388:G:A is among the strongest in Ferkingstad 2021 and is present in QTD000584,
the INTERVAL data for the same aptamer) has an 88-row deCODE pseudo-CS file holding one
CFH locus on chr1 (−log10 p 8.9) and one chr2 locus, and nothing on chr12. IL18
seq.5661.15 likewise: 94 rows, CFH only, no cis. This is not a platform-discordance case
(same aptamer across INTERVAL, FinnGen SomaScan and deCODE), so the chr12 rows are either
absent from `seq.10361.25.munged.tsv.gz` (step 0) or were lost in the shard (step 1).
Check the munged file at chr12:112.85–112.99 Mb directly; if the rows are there, rerun
that one shard locally and compare.

The scale of this class is unknown. A cross-platform comparison overstates it (445 of
1,396 proteins with a UKB-PPP Olink cis-pQTL have a deCODE aptamer but no deCODE cis set,
220 of them with Olink −log10 p > 100, e.g. KIR2DS4, CD300LF, IFNGR2, FGFR4, PON1 —
mostly plausible SomaScan/Olink discordance per Eldjarn 2023). A same-platform
comparison understates it (only 4 aptamers have a FinnGen SomaScan cis set and a deCODE
report without one, all weak in FinnGen). The right reference is Ferkingstad's own
sentinel table (Supplementary Table of the 18,084 primary associations, cis flagged):
every published cis sentinel should have a deCODE pseudo CS within ±1 Mb, and the misses
are the list to explain.

## 4. Hundreds of satellite "credible sets" around every huge signal

The pseudo-CS pipeline turns one colossal pQTL into hundreds of loci. The munge doc
records the mechanism for COL6A1 (471 loci over 4 Mb): the dynamic r² threshold drops far
below the panel's listing floor, so every variant the panel does **not** list as a partner
— weak long-range LD, anything beyond the panel's window — becomes its own locus, and the
proximity filter only suppresses ±1 Mb around anchors with χ² > 250. Measured now:

| aptamer | leads | within 1 Mb of gene | same chr, 1–20 Mb | same chr, > 20 Mb | other chr | max −log10 p |
|---|---|---|---|---|---|---|
| IL6R seq.15602.43 | 532 | 1 (−log10 p 10,773) | 451 (median −log10 p 10.5, 197 singletons) | 73 | 7 | 10,773 |
| ERAP2 seq.8960.3 | 657 | 1 | 610 | 18 | 28 | 22,204 |
| CPA4 seq.9267.2 | 642 | 1 | 638 | 2 | 1 | 21,114 |
| VTN seq.13125.45 | 622 | 1 | 614 | 2 | 5 | 29,138 |
| MBL2 seq.3000.66 | 626 | 1 | 616 | 1 | 8 | 7,427 |
| NAGLU seq.15509.2 | 698 | 1 | 139 | 1 | 557 (555 on chr1, around a −log10 p 5,222 trans signal) | 5,222 |
| TST seq.12663.1 | 678 | 0 | 1 | 0 | 677 (647 on chr1 around a −log10 p 8,992 trans signal) | 8,992 |

Overall: 130,685 leads for 4,808 aptamers (mean 27, median 7, p90 62, max 698; 314 aptamers
with more than 100) against 18,084 primary associations in the paper (3.7 per aptamer).
67,013 leads are single-variant sets with pip 0.99. Any consumer that counts credible sets
— trans-pQTL counts, "independent signals" for MR, coloc inputs, locus views — is counting
LD shadows. Possible repairs, in increasing cost: suppress every lead within a radius that
grows with the anchor's χ² (the 1 Mb radius is far too small for χ² ≈ 10⁵, where even
r² = 0.001 gives χ² ≈ 100); keep only loci that match Ferkingstad's sentinel list; or
re-clump the strong aptamers with conditional analysis. Whatever is chosen, record the
rule in the dataset description, which today says only "not formally fine-mapped".

## 5. Pervasive trans hotspots to verify against the paper

1 Mb bins with the most aptamers carrying a lead: chr1:196 Mb CFH — **2,242 of 4,808
aptamers**, median −log10 p 24; chr3:165 Mb BCHE/unnamed — 1,173 + 572; chr6:31–32 Mb MHC —
1,293 + 787; chr17:7 Mb ASGR2 — 622 + 458; chr3:56 Mb ARHGEF3 — 583; chr17:4 Mb SPNS2 —
504; chr2:216 Mb IGFBP2 — 439; chr19:6 Mb C3 — 301; chr11:0 Mb ODF3 — 266; chr9:133 Mb
ABO — 333; chr19:44 Mb APOE — 272; chr17:28 Mb VTN — 309. ASGR1/2, ABO, APOE, VTN, SH2B3,
ARHGEF3 and the MHC are known SomaScan trans hotspots. CFH at nearly half of all aptamers
with a median −log10 p of 24, and BCHE at a quarter, need checking against Ferkingstad's
hotspot figure: if the paper does not show CFH as the dominant pleiotropic locus, suspect
the munge (a column or aptamer mix-up in the bucketed split, or a shared covariate
artefact in the source). Per-aptamer files for OAS1 and IL18 both show the CFH set at
chr1:196,677,046 with cs_size 82–93, so it is in the loaded data, not an aggregation error.

## 6. Semantics a consumer can misread

- `pip` is heuristic (relative association strength within an LD clump), not a posterior;
  the 0.99 on 67,013 singleton sets means "no partner was listed", not "causal".
- `aaf` is NULL for every row: the source carries only unoriented `maf`, which the munge
  discards. Keeping it as a `maf` column would at least allow MAF filters and the AF
  concordance check MR needs; it cannot give the effect-allele frequency.
- No per-variant `n` (source has it, ~35,3xx per row; dropped).
- `mlog10p` reaches 29,138; it cannot be turned into a p-value in double precision, and
  any code that does `10**-mlog10p` gets 0.
- `cs_min_r2` and membership are from the FinnGen R12 panel, i.e. Finnish LD for
  Icelandic associations; partners absent from the panel make singleton sets.
- 368 leads (max-PIP rows) have −log10 p < 7.3, so the max-PIP row is not always the
  clump lead; the lead is the variant named in `cs_id` (`chr1_111569518_T_C`).
- 28 traits are not current HGNC symbols in `gene_annotations_v` (C5orf38, FUT10 with
  362 sets, COL4A3BP, H2AW, H2BU1, N6AMT1, …) and 7 aptamers carry their id as the trait
  (seq.5981.6, seq.6633.43, seq.7143.9, seq.7995.16, seq.8355.80, seq.8383.20,
  seq.8927.6): cis/trans classification by symbol join silently fails for them.
- The 2023 alignment dropped 7 % of source rows (chr21: 9,564,833 → 8,863,739) and folded
  1,831 duplicate indel representations by max −log10 p; whether any cis sentinel was
  among the dropped rows is not recorded.

## 7. Not a defect, but worth stating in the dataset description

SomaScan v4 aptamer specificity: Eldjarn 2023 found cis support for 43 % of SomaScan
assays against 72 % of Olink assays, and many strong Olink cis-pQTLs (item 3) have no
SomaScan counterpart. A protein-level question answered from deCODE alone inherits that.
