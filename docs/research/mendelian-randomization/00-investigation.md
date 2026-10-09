# Mendelian randomization as a predefined analysis: investigation

Written 2026-10-08, before any planning epic exists. Input: the MR literature (links at the
end), the live daly BigQuery dataset (`daly-finngenie.genetics_results`), the SDK surface in
genetics-mcp-server, and the results-api configuration. Nothing here is decided; the
decisions are listed in the last section and the user picks.

## The one-paragraph answer

The data this suite holds is exactly the shape the 2024–2026 proteome-wide MR literature
uses: cis-pQTL instruments from UKB-PPP (Olink, n=42,853), deCODE (SomaScan, n=35,559) and
FinnGen (Olink 3K/5K, SomaScan), run against FinnGen disease endpoints, the FinnGen+MVP+UKB
meta-analyses, IIBDGC, PGC, COVID-HGI and GP2 — plus fine-mapped polygenic exposures
(Open Targets 26.09, the UKB NMR metabolome, FinnGen lab values) for trait-level MR. The
analysis can be built as an SDK module (`genetics.mr`) in the same way `linemodels` and
`enrichment` were: a numpy port of the deterministic estimators held to R reference
fixtures, then replications of published results run live. Three things constrain the
design and must be settled before subtasks exist: (1) the LD proxy returns only r² and D',
never signed r, so a correlated-instrument IVW is not possible without a cross-repo change;
(2) the credible sets in the data are not all independent signals — deCODE's pseudo
credible sets list ~60 single-variant "sets" per protein per chromosome and FinnGen's SuSiE
splits one strong pQTL into several — so instrument selection cannot trust `cs_id`;
(3) a proteome-wide screen (2,000 proteins × one outcome) is a different product from a
per-target analysis and probably does not fit a 120 s sandbox run.

## What the literature does

### The established references

| Paper | Design | What to take from it |
|---|---|---|
| Zheng et al. 2020 *Nat Genet* (phenome-wide MR of the plasma proteome) | 1,002 proteins × 225 phenotypes, cis and trans | Instrument rules: p ≤ 5e-8, MHC excluded, clump r² < 0.001, cis = ±500 kb of the leading pQTL; Wald ratio for one instrument, generalized IVW with an LD matrix for several cis instruments; coloc PP4 > 0.8 as the LD-confounding filter; **31.5 % of MR-supported associations failed colocalization**; tier 3 = instruments associated with > 5 proteins, excluded outright; named cis targets IL6R→RA/CHD, PCSK9→hypercholesterolaemia, IL23R→Crohn's (OR 3.22), IL12B→psoriasis, TNFRSF11A→osteoporosis, AGT→hypertension, PLAU→IBD |
| Burgess et al. 2023 *Wellcome Open Res* (guidelines, v3) | Methods guidance | IVW with multiplicative random effects as the primary estimator unless very few variants or one gene region; sensitivity trio MR-RAPS, weighted median, MR-cML; MR-Egger is "imprecise, often low power"; MR-Mix has a "very high false positive rate"; Steiger filtering; positive and negative control outcomes; "a single-gene analysis cannot use the robust methods, so other sensitivity analyses are needed"; "there is some danger in a post hoc or data-driven selection of genetic variants"; estimates "should not generally be interpreted directly as the expected impact of intervening on the exposure" |
| Karhunen et al., practical guide to cis-MR (*AJHG*, PMC13277696) | cis-MR tutorial, APOC1 worked example | Prefer **one well-justified variant** over many; window ±100 kb common, up to ±250 kb, gene-only in dense regions; pleiotropy-robust methods "unlikely to be particularly informative" in one region; colocalization needs PP.H3+PP.H4 > 0.5 to be informative; check LD to variants in neighbouring genes (their GIPR example vanished after conditioning on APOE); protein-altering variants can change aptamer/antibody binding without changing abundance; "avoid high-throughput, purely algorithmic application across many proteins" |
| Zhang et al. 2025 *Nat Commun* 16:2813 (UKB-PPP proteins → IBD) | 2,923 proteins; UKB-PPP cis-pQTL → IIBDGC + FinnGen R9, MR estimates meta-analysed | cis = ±1 Mb, p < 5e-8, clump r² < 0.001 against 1000G EUR; **1,976 proteins had cis instruments**; Wald ratio or IVW; FDR < 0.05; coloc PPH4 ≥ 0.6; Steiger; reverse MR; tier-1 hits IL12B(+), CD6(+), MXRA8(−) for IBD, CXCL9/IL12B/IFNG/CCN3/RSPO3 for CD, IL18 for UC; full per-protein table in Supplementary Data 9 |
| Zhou et al. 2021 *Nat Med* (OAS1 → COVID-19) | cis-pQTL (INTERVAL aptamer 10361.25) → HGI outcomes | OR per SD OAS1: 0.54 death/ventilation, 0.61 hospitalization, 0.78 susceptibility; the instrument is the splice variant rs10774671 |
| Schmidt et al. 2020 *Nat Commun* 11:3255 (drug-target MR) | Framework | Proteins make the no-pleiotropy assumption more plausible; the cis region is the drug target |
| Burgess, Davies, Thompson 2016 *Genet Epidemiol* | Sample overlap | Bias from overlap scales with the overlap fraction and the weak-instrument bias; in case-control outcomes it depends on whether the exposure was measured in cases |
| Variant selection in cis-MR (PMC12887808, 2025) and cis-MRBEE (BiB 2025) | Correlated instruments | Lead-only loses power when there are several signals; LD-pruning, COJO, SuSiE and PCA all need an LD reference; SuSiE picks "informative" variants |
| MendelianRandomization R 0.9.0 (PMC10616660) | Reference implementation | Fixed effects by default with ≤ 3 variants, random with ≥ 4; correlated IVW is generalized weighted least squares with the **signed** correlation matrix; PC-GMM for many correlated variants |

The 2025–2026 applied papers (AF/flutter, lung cancer, COPD, gastric and bladder cancer,
ITP, pelvic organ prolapse, cardiac conduction disorders) all run the same pipeline:
UKB-PPP and deCODE cis-pQTLs as exposure, a FinnGen R10–R13 endpoint as outcome, Wald/IVW,
FDR, coloc, Steiger, reverse MR, and a second platform as replication. Two recurring
observations in them matter for the design: the overlap of hits between UKB-PPP and deCODE
is small (44 vs 14 proteins for AF, 3 shared), and colocalization usually removes most of
what MR flags (AF: only PCSK9 survived). A module that reports MR without the coloc and
LD checks reproduces the part of those papers that reviewers distrust.

### What a careful implementation has to contain

From the guidelines and STROBE-MR, the items a result must carry, in the order a reader
needs them: the instrument set and how it was chosen (window, threshold, independence rule,
what was excluded and why); the harmonised per-variant table (effect allele, both betas,
both SEs, both allele frequencies, PIP, F); the primary estimate with its units
("per genetically predicted SD of protein", "log OR"); heterogeneity (Q, I²);
leave-one-out; for polygenic exposures the robust estimators and the Egger intercept; for
cis the LD/coloc evidence instead; Steiger direction; the sample-overlap statement; the
positive and negative controls that were run; and the caveats that cannot be tested.

## What our data supports

### Exposures: where instruments can come from

Instruments are read from `credible_sets_v` (beta, se, pip, cs_id, aaf, GRCh38
`chr:pos:ref:alt`, alt = effect allele). Counts on daly, 2026-10-08:

| dataset | fine-mapping | traits | credible sets | proteins with a cis lead (±500 kb of the coding gene) |
|---|---|---|---|---|
| UKB_PPP (ukbb) | SuSiE, aaf present | 2,646 proteins | 41,726 | 2,020 |
| deCODE_pQTL_2021 | **pseudo** (LD clumps of p<0.005 sumstats), aaf NULL | 4,572 aptamers | 129,039 | 1,838 |
| FinnGen_Olink (3K) | SuSiE | 1,849 | 6,058 | 1,281 |
| FinnGen_Olink_5K | SuSiE | 2,019 | 4,678 | 1,269 |
| FinnGen_SomaScan (n=1,000) | SuSiE | 2,945 | 5,002 | 876 |
| eQTL Catalogue QTD000584 (INTERVAL SomaScan via eQTL Catalogue) | SuSiE | — | — | holds OAS1 (see below) |
| Open_Targets_26.09 | SuSiE (non-FinnGen studies), **aaf NULL** | 21,850 studies | 739,423 | polygenic exposures: LDL (GCST90691895 etc.), BMI, blood pressure, AD … |
| nmr_ukbb_est | SuSiE (UKBB EUR subset) | 249 NMR traits | 123,899 | LDL_C, HDL_C, Total_TG … in SD units; 5,004 rows with NULL beta/se |
| FinnGen_R13_MVP_UKBB_labs / FinnGen_R13_UKBB_labs | pseudo | 46 / 48 lab traits | 99,613 / 60,450 | lipids, HbA1c, eGFR … (FinnGen samples inside) |
| FinnGen_R14, FinnGen_kanta, FinnGen_drugs | SuSiE | 1,423 / 328 / 126 | | binary and lab exposures (FinnGen samples) |

The UKB-PPP count (2,020 proteins with a cis lead) matches Zhang 2025's 1,976 within what a
±1 Mb vs ±500 kb window and p-threshold differences explain, so the credible-set route to
instruments is at least complete for that platform.

### Outcomes: where the instruments can be looked up

`summary_stats(variants=…, phenotypes=…)` serves per-variant rows (beta, se, af, pval,
mlog10p; af_cases/af_controls for FinnGen) by byte-range reads over tabix-indexed GCS files.
Outcomes are therefore limited to the datasets with summary statistics: **FinnGen R14
endpoints, kanta labs, drugs, FinnGen HLA; FinnGen+MVP+UKB and FinnGen+UKB meta-analyses
(disease and labs); FinnGen Olink and the FinnGen+UKB-PPP pQTL meta; IIBDGC (IBD, CD, UC);
PGC SCZ and BIP; COVID-HGI A2/B1/B2/C2; GP2 PD; FinnGen snRNA eQTL; deCODE ASM-QTL.**
Open Targets, deCODE pQTL, UKB-PPP, INTERVAL, SomaScan and NMR have **no** summary
statistics here, so they can only be exposures, and reverse MR (disease → protein) is only
possible for the FinnGen Olink / FinnGen+UKB-PPP pQTL sumstats.

Sample sizes for the obvious replication outcomes (from `phenotypes_v`): I9_CHD 94,024 cases /
405,731 controls; T2D 95,803 / 389,689; I9_HYPTENS 169,629 / 330,045; E4_HYPERCHOL 54,332 /
403,698; L12_PSORIASIS 13,888 / 479,791; M13_RHEUMA 17,099 / 302,247; M13_OSTEOPOROSIS
12,790 / 468,746; G6_ALZHEIMER 18,829 / 480,926; IIBDGC IBD 125,992 / 1,262,229, CD 59,734,
UC 57,673; COVID_A2 18,152 cases, B2 44,986, C2 159,840. (FinnGen K11_IBD, K11_CROHN and
K11_ULCER exist in `phenotypes_v` under other codes; check `search(kind="phenotypes")`.)

### Sample overlap, by construction

| exposure | outcome | overlap |
|---|---|---|
| UKB_PPP, deCODE | FinnGen R14, IIBDGC, PGC, GP2, COVID-HGI | none (deCODE) / none unless the outcome meta includes UKB (COVID-HGI and IIBDGC do) |
| UKB_PPP | FinnGen_R13_UKBB*, FinnGen_R13_MVP_UKBB* | UKB participants in both |
| FinnGen Olink/SomaScan, FinnGen labs/kanta, FinnGen R14 credible sets | any FinnGen outcome | one-sample; the pQTL cohorts are small (1–4 k of 500 k) so the overlap fraction is tiny, but the Olink 3K cohort was **selected on carrier status of variants of interest** |
| nmr_ukbb_est, Open Targets UKB studies | FinnGen+UKB metas | UKB in both |

The module can derive this table from `datasets.yaml` (cohort membership) rather than ask
the user, and must print it with every estimate.

### LD

The only LD source is the FinnGen LD server (SISu 4.2 panel) through results-api's proxy:
pairs or a neighbourhood (window 100 kb – 5 Mb) above an r² threshold, returning **r² and
D' only**. Consequences:

- Generalized IVW with a signed correlation matrix (Zheng 2020's multi-cis estimator,
  `mr_ivw(correl=…)`) **cannot be computed**. Sign of r could be recovered from D' and the
  allele frequencies only when D' is informative, which is not a route to rely on.
- r² is enough to **prune** instruments to independence (r² < 0.01 or 0.001) and to find a
  proxy for an instrument missing from the outcome.
- The panel is Finnish. Pruning deCODE or UKB-PPP instruments against Finnish LD is the
  usual cross-ancestry compromise European-panel papers also make; variants absent from
  the panel come back 404 and must be reported as "LD unknown", not treated as independent.
- A neighbourhood scan per instrument costs one request; the per-execution caps are 1,000
  requests, 4 in flight, 120 s wall clock, 2,560 MiB. A cis analysis (≤ 10 instruments) is
  cheap; pruning 60 deCODE leads per protein across 2,000 proteins is not.

### Colocalization already exists for the main pair

`colocalization_v` holds precomputed coloc for FinnGen_R14 × UKB_PPP (139,120 rows,
124,528 with PP.H4 > 0.8 — stored only where both have credible sets), × FinnGen Olink 3K/5K,
× SomaScan, × INTERVAL and × QTD000584. The MR module can attach the coloc row for
(protein credible set, outcome credible set) when the outcome is FinnGen R14 and say
"not computed" otherwise. That is the single most important sensitivity check in the
literature and it costs one BigQuery query.

### Execution envelope

`run_analysis` is capped at 120 s, 1,000 requests, 4 concurrent, 2,560 MiB address space,
and a 25,000-row cap on `sql()`. One protein → one outcome is a handful of calls. One
protein → all 1,423 FinnGen endpoints is one `summary_stats` call per phenotype file
(results-api fans out per phenotype), i.e. roughly a thousand byte-range reads: probably
feasible but unmeasured. 2,000 proteins → one outcome is one BigQuery query for the
instruments plus one summary-stats call with ~10,000 variants — the response size and the
LD-pruning requests are the unknowns. These numbers need measuring on staging before
scope is fixed; see the decisions.

## Replication targets, in the order they retire risk

1. **Estimator identity** (no live data): R `MendelianRandomization` 0.9+ and `TwoSampleMR`
   outputs on fixed harmonised tables — IVW fixed/random, MR-Egger (slope, intercept, I²_GX),
   weighted median (seeded bootstrap SE), simple/weighted mode, Cochran's Q, leave-one-out,
   Steiger (r² from beta/se/af/n), contamination mixture if included — generated in a pinned
   `rocker/r-ver` image exactly as `tests/fixtures/linemodels` and the enrichment branch's
   fixtures were. Deterministic estimators held to ~1e-12; bootstrapped ones within their
   Monte Carlo noise. This is cheap and comes first.
2. **Drug-target cis-MR with known answers**, UKB-PPP (and deCODE where present) → FinnGen
   R14, all run through the real SDK path: PCSK9 → E4_HYPERCHOL and I9_CHD (protective
   per SD; the UKB-PPP missense lead 1:55039974:G:T carries beta −1.18); IL6R → M13_RHEUMA
   and I9_CHD (Zheng tier 2; rs2228145 shedding caveat); IL23R → Crohn's (OR 3.22 in Zheng);
   IL12B → L12_PSORIASIS; TNFRSF11A → M13_OSTEOPOROSIS; AGT → I9_HYPTENS; OAS1 (QTD000584
   aptamer 10361.25, splice variant 12:112919388:G:A) → COVID_A2/B2/C2 with Zhou 2021's
   0.54 / 0.61 / 0.78 per SD as the anchor. Direction must match in every case; magnitudes
   within the published CI where the outcome is the same cohort family.
3. **A whole published screen on the same exposure data**: Zhang 2025's UKB-PPP cis-pQTL →
   IBD/CD/UC with IIBDGC (we hold it) and FinnGen (R14 here vs R9 there). Compare our
   per-protein Wald/IVW estimates with their Supplementary Data 9 for the IIBDGC arm — same
   exposure, same outcome, so the correlation of estimates should be close to 1 and the
   tier-1 proteins (IL12B, CD6, MXRA8, CXCL9, IFNG, CCN3, RSPO3, IL18) must reproduce in
   direction. A low correlation here is a harmonisation or instrument-selection bug, not
   biology. This is the measurement that can kill the design.
4. **Trait-level positive and negative controls**: LDL-C (nmr_ukbb_est LDL_C, or Open
   Targets GCST90691895) → I9_CHD, expected OR ≈ 1.4–1.5 per SD; BMI (Open Targets) → T2D,
   expected ≈ 2.5 per SD; SBP → stroke ≈ 1.35 per 10 mmHg; CRP → I9_CHD null; HDL_C → I9_CHD
   should show the known pleiotropy pattern (IVW positive, attenuating under robust
   estimators); Burgess-style negative control outcomes (hair colour, skin tanning) are not
   in FinnGen, so use an endpoint with no plausible link instead. Exact anchors to be pinned
   to the primary papers at implementation time.
5. **Reverse direction and Steiger** on the FinnGen Olink sumstats: disease → protein for
   the tier-2 hits, as Zhang 2025 did.

Replication-grade fixtures that come from papers' supplementary tables are **untrusted
downloads** and go into their own directory with a checksum, as the project's download rule
requires. Downloaded 2026-10-09 into `~/mr_fixtures_untrusted/` on the daly dev VM (not in
any repo): Zhang 2025 Supplementary Data 1–23 (one workbook; Data 9 is the IIBDGC/FinnGen/
Combined per-protein OR table for IBD, CD and UC), Zheng 2020 Supplementary Tables 1–23
(instruments, multi-cis MR, drug-target predictions with tiers) and Zhou 2021 Supplementary
Tables 1–12 (per-protein MR on COVID A2/B2/C2, OAS1 SOMAmer 10361.25 Wald ratio OR 0.544 for
A2); `MANIFEST.md` there says which sheet holds what and `SHA256SUMS` pins the files.

## Corner cases and hazards, with the evidence seen in our data

- **Credible sets are not independent signals.** FinnGen_Olink IL6R has three single-variant
  credible sets (cs 1, 4, 7; beta 1.11–1.15, aaf 0.30, within 10 kb) that are one signal
  SuSiE split. deCODE's pseudo credible sets for IL6R list ~60 single-variant sets with
  pip 0.99 across chr1 111–120 Mb, because the pseudo-CS pipeline clumps p<0.005 sumstats
  with a per-lead dynamic r² threshold. **Rule:** one lead per credible set, then prune by
  pairwise r² (< 0.01) through the LD proxy, and refuse pseudo credible sets as
  "independent" without that step. Report how many leads survived and why each was dropped.
- **Protein-altering variants as instruments.** UKB-PPP's strongest PCSK9 instrument is the
  R46L missense (beta −1.18 per allele), IL6R's is rs2228145 (D358A, beta 1.11): both are
  real biology (lower PCSK9, more soluble IL6R) but the cis-MR guide and Sun 2023 both warn
  that missense variants can alter binding. **Rule:** flag `most_severe` in the coding gene
  and offer a non-coding-only sensitivity run; never silently drop.
- **Scale and units.** Protein betas are per SD of rank-normalised level; NMR betas are per
  SD; FinnGen binary outcomes are log OR; kanta/lab outcomes are per unit or SD depending on
  the trait; FinnGen_R14 "Height, inverse-rank normalized" exists next to raw traits. The
  estimate must name both units or refuse. `n_samples`/`n_cases` come from `phenotypes_v`
  and `datasets.yaml`, and are missing for some datasets (COVID-HGI per-arm n is there;
  deCODE n is in the registry; Open Targets n is not) — F-statistics and Steiger then
  degrade to "not computable", stated.
- **Allele harmonisation.** Both sides are GRCh38 `chr:pos:ref:alt` with beta on alt, so a
  join is exact and there is no strand ambiguity in the usual sense — but deCODE was
  re-normalised to gnomAD ref/alt by the munge and Open Targets and deCODE carry **no
  allele frequency** of their own. The concordance check (the one test that catches a
  flipped effect allele) uses, in this order: the outcome's own per-phenotype `af_alt` from
  the FinnGen summary-statistics rows (correct for that phenotype's samples, the user's
  ruling 2026-10-09), the exposure's AF where it has one (UKB-PPP, FinnGen Olink, NMR), the
  FinnGen variant annotation's AF (`genetics.variant_annotation(..., source="finngen")`) for
  a FinnGen variant with no row, and gnomAD `AF_nfe`/`AF_fin` (`source="gnomad"`) only for a
  non-Finnish exposure variant that nothing FinnGen-side covers. gnomAD is not the reference
  for FinnGen variants. The LD panel files carry no AF. The module must say which
  instruments were AF-checked against which frequency and which could not be.
- **Missing outcome rows.** An instrument absent from the outcome file (rare in Finns,
  Icelandic-specific, failed QC) needs a proxy (r² > 0.8 neighbour that is present) or an
  explicit drop; the FinnGen panel will not have every deCODE variant either.
- **MHC.** Exclude chr6:25–34 Mb instruments by default (every paper does), report the count.
- **Winner's curse.** Instruments chosen and weighted in the same pQTL study overestimate
  the exposure association; Burgess recommends a three-sample design when a second pQTL
  study exists — here UKB-PPP and deCODE/FinnGen Olink can serve as selection vs weighting
  samples for shared proteins. Worth offering, not default.
- **One-sample analyses inside FinnGen.** FinnGen R14 credible sets → FinnGen R14 endpoints
  are one-sample with winner's curse and the Olink 3K cohort is ascertained on genotype.
  Permit with a loud statement; never present as two-sample.
- **Binary exposures** (a FinnGen endpoint as exposure): the estimate is per log-odds unit
  of liability and the guidelines say two-stage logistic approaches are fragile. Allow,
  label, and point to the guideline wording.
- **Heterogeneity in cis is expected** when several signals exist (co-regulation, Tambets et
  al.), so Q in a cis analysis is information, not a verdict.
- **Multiple testing in screens**: PhenoSpD-style effective-test counts (Zheng) vs FDR
  (Zhang); the module should report raw p and let the caller choose, with the number of
  tests actually run.
- **The analysis that is wrong by construction**: "MR" from a trans-pQTL at a pleiotropic
  locus (ABO, APOE, FUT2, the complement cluster) or from instruments associated with > 5
  proteins (Zheng tier 3). Count how many proteins each instrument is a credible-set lead
  for (one BigQuery query) and refuse or flag past a threshold.

## Data findings that fall out of this (recorded, not filed)

- **deCODE has no OAS1 cis pseudo credible set.** Aptamer seq.10361.25 (OAS1) has clumps on
  chr1 and chr2 only, and the OAS1 region (chr12:112.85–112.99 Mb) holds deCODE sets for
  MMAB and SPINT1 but not OAS1, although OAS1's cis-pQTL at rs10774671 is among the
  strongest in Ferkingstad 2021 and is present in QTD000584. Either the munged p<0.005 file
  lacks the region or the pseudo-CS step dropped it. The replication in step 2 will show
  whether this is one protein or a class. Unverified from this machine (the source bucket
  is finngen-commons).
- FinnGen_Olink IL6R is split into three near-identical credible sets (above); any consumer
  that counts credible sets as signals over-counts there.
- UKB_PPP has no OAS1 trait (likely not on the Olink 3072 panel); the only OAS1 cis instrument
  in the suite is the eQTL-Catalogue INTERVAL aptamer.

None of these block the MR work and none is filed; they go into the epic's standing batch
bead when the epic exists.

## Proposed shape (for the architecture-explorer to argue with)

Mirror `linemodels` and `enrichment`: a pure-numpy/polars module `genetics.mr` with
composable functions, one orchestrating entry point, every default echoed back, and the
figure in `genetics.plots`.

```
res = genetics.mr.run(
    exposure={"resource": "ukbb", "dataset": "UKB_PPP", "trait": "PCSK9"},
    outcome={"resource": "finngen", "phenotype": "I9_CHD"},
    instruments="cis",            # "cis" | "genome_wide" | a frame the caller built
    cis_window=500_000, p_threshold=5e-8, r2_prune=0.01, exclude_mhc=True,
)
res["estimate"]      # method, beta, se, p, 95% CI, exposure_unit, outcome_unit, n_instruments
res["instruments"]   # harmonised table: variant, beta_x, se_x, af_x, beta_y, se_y, af_y, pip,
                     #   cs_id, F, consequence, proxy_used, dropped_reason, r2_to_kept
res["sensitivity"]   # per-method estimates; Q, I2; leave-one-out; Egger intercept
                     #   (genome_wide only); Steiger; coloc row when precomputed
res["checks"]        # named checks with pass / warn / fail / not_computable and one line each
res["parameters"]    # every default that was used, sample overlap statement, data versions
```

`genetics.mr.instruments(...)`, `harmonise(...)`, `estimate(frame, method=...)` are the
pieces, so a script can substitute its own instruments (an uploaded table) and still get
the checks. `genetics.plots.mr(res)` draws the scatter with the fitted lines, the forest of
Wald ratios and the leave-one-out panel. The checklist the result prints is STROBE-MR's,
and the docstring states what the module does **not** do (correlated-instrument IVW,
MVMR, coloc on demand, CAUSE/MR-APSS) and why.

Registration is the known seven-place checklist from the linemodels memory (lazy modules,
executor `_SDK_MODULES`, import-closure test, `prune_venv.py` allow-list, `SURFACE_MODULES`
in `gen-sandbox-docs.py`, the `module` enum in `tools/definitions.py`, the hand-copied
descriptions in `docs/chat-tool-reference.md`) plus the data-analysis skill instruction and
the project-spec section the enrichment branch added.

## Decisions the user has to make

1. **Scope of exposures in v1.** (a) cis-pQTL / drug-target MR only; (b) also polygenic
   trait MR from any fine-mapped GWAS (Open Targets, NMR, FinnGen). The estimators are
   shared, but the valid sensitivity analyses, the instrument rules and the replication
   set differ. Recommendation: both, with an explicit `instruments=` mode and
   mode-specific reporting, because the trait-level positive controls (LDL → CHD) are the
   cheapest test of harmonisation and they need the genome-wide path anyway.
2. **Correlated instruments.** Accept r²-only LD and independent instruments in v1
   (recommended), or first extend the LD proxy and the upstream LD server to return signed
   r (a cross-repo change in results-api and possibly outside the suite) so that
   multi-signal cis-MR can use the generalized IVW. The literature increasingly argues for
   a single well-justified cis variant anyway.
3. **Proteome-wide screens.** On-demand inside one sandbox run (needs the staging
   measurement above and may not fit 120 s), a precomputed table like `colocalization_v`
   (a munge/db epic), or out of scope for v1 with the module documented as per-target.
   Recommendation: per-target plus "one exposure → many outcomes" in v1, measure the
   screen on staging as a subtask, and decide the precomputed table from the number.
4. **Proving ground.** Staging (daly-finngenie, the only writeable cluster) is reachable
   from this machine and has the data; the R reference image needs Docker on this host
   (present). The published supplementary tables for step 3 must be downloaded and
   checksummed into fixtures. Nothing else is needed, so the proving ground is available
   today — but the deCODE OAS1 gap means step 2 may surface data-ingestion work in the
   munge repo that is outside this module.
5. **Kill criterion.** Proposed: if, after harmonisation is fixed, the UKB-PPP → IIBDGC
   per-protein estimates do not correlate with Zhang 2025's Supplementary Data 9 at
   r > 0.9, or LDL → CHD does not land in 1.3–1.7 per SD with the PCSK9/IL6R directions
   right, stop and treat the data route (credible sets as instruments) as unfit rather than
   harden the module.

## Sources

- Zheng J et al. 2020, *Nat Genet* 52:1122 — https://doi.org/10.1038/s41588-020-0682-6 (PMC7610464)
- Burgess S et al. 2023, Guidelines for performing MR investigations, v3 — https://doi.org/10.12688/wellcomeopenres.15555.3 (v2 text: PMC7384151)
- Karhunen V et al., practical guide to cis-MR — https://pmc.ncbi.nlm.nih.gov/articles/PMC13277696/ ; script https://github.com/vkarhune/cisMRguide
- Zhang X et al. 2025, *Nat Commun* 16:2813 — https://doi.org/10.1038/s41467-025-57879-3 (PMC11928603)
- Zhou S et al. 2021, *Nat Med*, OAS1 and COVID-19 — https://pubmed.ncbi.nlm.nih.gov/33633408/
- Ferkingstad E et al. 2021, *Nat Genet* 53:1712 — https://doi.org/10.1038/s41588-021-00978-w
- Sun BB et al. 2023, *Nature* 622:329 (UKB-PPP) — https://doi.org/10.1038/s41586-023-06592-6
- Eldjarn GH et al. 2023, *Nature* 622:348 (platform comparison) — https://doi.org/10.1038/s41586-023-06563-x
- Schmidt AF et al. 2020, *Nat Commun* 11:3255 — https://doi.org/10.1038/s41467-020-16969-0
- Burgess S, Davies NM, Thompson SG 2016, sample overlap — https://pmc.ncbi.nlm.nih.gov/articles/PMC5082560/
- Skrivankova VW et al. 2021, STROBE-MR, *JAMA* 326:1614 and *BMJ* 375:n2233
- Variant selection in cis-MR (2025) — https://pmc.ncbi.nlm.nih.gov/articles/PMC12887808/
- cis-MRBEE (2025, *Brief Bioinform*) — https://pubmed.ncbi.nlm.nih.gov/40471990/
- MendelianRandomization v0.9.0 — https://pmc.ncbi.nlm.nih.gov/articles/PMC10616660/
- Lyall DM et al. 2017, *JAMA Cardiol*, BMI → T2D OR 2.53 per SD in UK Biobank — https://pmc.ncbi.nlm.nih.gov/articles/PMC5710596/
- 2025 applied screens with FinnGen outcomes: AF/flutter https://pmc.ncbi.nlm.nih.gov/articles/PMC12751433/ ; cardiovascular (Advanced Genetics) https://pmc.ncbi.nlm.nih.gov/articles/PMC12245532/ ; lung cancer https://pmc.ncbi.nlm.nih.gov/articles/PMC12592614/
