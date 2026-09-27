# Literature-scrutiny review: bneale-0.txt (17 sessions, 33 assistant messages, 2026-04-15 → 2026-09-23)

Read in full (4,199 lines / 604 KB). Every assistant message that reports literature was checked; where a RESULT block exists the claim was compared to the persisted source text.

## 1. Counts

| Item | Count |
|---|---|
| `search_scientific_literature` calls | **37** (15 old-format `*[Using tool…]*`, 22 `[[TOOLUSE…]]`) |
| …by backend | **37 perplexity, 0 europepmc** (every USER line carries `lit_backend=perplexity`; no session ever switched) |
| `web_search` calls | 1 (CHRM4 session, duckduckgo, FDA approval of Cobenfy) |
| Literature calls whose RESULT is persisted | 21 of 37 (+ the web_search). 16 calls in the Apr–May sessions and in the first turn of four later sessions have no RESULT — judged from text alone |
| Records inside persisted Perplexity RESULTs | 115 records: 87 with `metadata_source: europepmc` (PMID/DOI present), 28 `metadata_source: perplexity` (no PMID/DOI, "abstract" is a page snippet). Each RESULT also carries a free-text `summary` field, which the persistence layer truncates (see GPR17) |
| Assistant messages citing literature with **no** tool call in that message | 7 (e0bf5d26/27376b50, 805b8c2f/475e5581, 805b8c2f/20eebc33, f28b4c65/4e511fb6, 3d6deffa/74c49424, 17201e6a/3db3e4e7, 925c7984/d928adf0) |
| Findings F1–F14 by category | cat 1: 5 · cat 2: 3 · cat 3: 4 · cat 4: 2 · cat 5: 1 · cat 6: 1 · cat 7: 8 · cat 8: 2 (a finding may carry several) |
| Severity | high 1 · medium 8 · low 5 |
| Counter-examples G1–G15 | 15 (concentrated in the Jul–Sep sessions) |
| User pushback on a literature claim inside this file | **0** explicit disputes. Closest: 3a7a000d "and what about ANTXR2 as a potential competing claim" (a probe that made the assistant retract a *data* claim). Both messages in 805b8c2f carry `thumbs=0` |
| Borderline cases skipped | ~24 citations (listed in §5) that appear in assistant text but cannot be matched to a RESULT because the RESULT block is truncated mid-list; plus one (Rietschel 2012 details) where the snippet is too short to check |

## 2. Findings

- **[F1] session=e0bf5d26 msg=27376b50-6b0d-4f4b-9919-96d31d7ee280 time=2026-04-15 16:56:53 user=bneale backend=none**
  - Question: How many unique fine-mapped protective LoF variants … with PIP > 0.05, MAF < 0.05, p < 1e-10 and what phenotypes?
  - Claim: "## PASS 2 — LITERATURE CONTEXT / The most biologically notable hits align well with established literature: **ANGPTL4** … validated in large-scale human genetics and clinical trials … **MAP3K15** … a recently described drug target … **CFHR5** … LoF protective against macular degeneration"
  - Source: no literature call; no citation anywhere in the section
  - Problem: 7 — an entire "literature" pass written from memory with zero references; several bullets (CFHR5, ICAM1) merely restate the loaded association as if it were literature, so the reader cannot tell what is corroborated and what is the same data described twice.
  - Severity: low

- **[F2] session=c22d6338 msg=28894dc7-c2a2-4288-bb9f-c4395027e506 time=2026-05-05 16:11:32 user=bneale backend=perplexity**
  - Question: what is the relationship between HNF4A and MASH
  - Claim: "#### 1. Molecular/Functional Role (Strong, from literature) … The literature consistently shows that **HNF4α loss-of-function promotes MASH progression**, while **HNF4α overexpression is hepatoprotective**" and "HNF4A+ cells increased from 17% in healthy liver to 24–42% in NASH patients"
  - Source: not persisted
  - Problem: 1, 4, 8 — biopsy series reported without n; an AAV-overexpression mouse study and a Chinese Medical Journal review are summed to "Strong" while the message's own table grades them "Moderate (animal models)" / "Preclinical only". Also "gallstones in Taiwanese men (OR=1.90; PMID:38058101)" is a single-population association with no n. Contrast in the same message: "**The rs1800961 variant is not associated with NASH in FinnGen (p=0.49).**"
  - Severity: medium

- **[F3] session=c22d6338 msg=45ce411b-da49-4da7-887c-acd1ed2a6c19 time=2026-05-05 16:27:06 user=bneale backend=perplexity**
  - Question: what can you tell me about hnf4a and diabetes
  - Claim: "This is one of the **largest common/low-frequency variant effect sizes** for T2D in the genome." and "Common variants at HNF4A explain only ~1.15 sibling relative risk (vs. epidemiological ~3.0)"
  - Source: not persisted; the first sentence has no citation at all
  - Problem: 7 — a genome-wide superlative asserted from memory; the λs figure is a relayed number with no way to check which cohort it came from.
  - Severity: low

- **[F4] session=805b8c2f msg=475e5581-75fa-4e13-8a9a-487e6f45d417 time=2026-05-08 18:59:44 user=bneale backend=none**
  - Question: frequency of the class of associated variants in significant genes from autism schizophrenia and bipolar disorder in gnomad
  - Claim: "the **well-known psychiatric risk genes** for these three disorders (e.g., from rare variant/exome studies: *SHANK3*, *NRXN1*, *SCN1A*, *CNTNAP2* for autism; *DISC1*, *DTNBP1*, *NRG1*, *COMT*, *SETD1A*, *GRIN2A* for schizophrenia; *ANK3*, *CACNA1C* for bipolar disorder)"
  - Source: no literature call
  - Problem: 5, 7 — DISC1, DTNBP1, NRG1 and COMT are candidate-gene-era schizophrenia genes with no support in PGC3 or SCHEMA, listed as "well-known … from rare variant/exome studies" next to SETD1A/GRIN2A with no distinction. The message was interrupted and the user re-asked with a correct gene list (RB1CC1, AKAP11, DOP1A, SETD1A, ZMYM2); both messages got `thumbs=0`.
  - Severity: medium

- **[F5] session=805b8c2f msg=20eebc33-694b-4b12-a247-223ca42eb183 time=2026-05-08 19:04:19 user=bneale backend=none**
  - Question: combined high confidence LoF allele frequency for RB1CC1 AKAP11 DOP1A SETD1A and ZMYM2 from gnomad
  - Claim: "**AKAP11 (o/e = 0.246)** … is a recently identified high-confidence bipolar disorder gene (LoF carriers have ~5× increased risk of BD)."
  - Source: no literature call
  - Problem: 7 — no citation; the published figure the assistant itself quotes four months later (f1b3a5a3) is OR 7.06.
  - Severity: low

- **[F6] session=06e7a264 msg=4aa44873-f22e-4096-b385-495e645f2054 time=2026-05-08 23:56:50 user=bneale backend=perplexity**
  - Question: can you provide a review of genome-wide association studies of behavioral traits
  - Claim: "All searches were conducted using both the **Perplexity** and **Europe PMC** backends." and "A 2026 Rutgers-led GWAS meta-analysis (>2.2 million individuals) found that most addiction risk derives from **broad genes affecting reward processing and impulse regulation** … [(News-Medical, 2026)]"
  - Source: not persisted; all seven calls in the message are `backend: perplexity`
  - Problem: 7, 8 — the provenance statement is false; a press release is cited as the source for a headline result. Also "[PGC-BD GWAS, ~2019]" links to a faculty-profile page, and "Okbay et al. (2022) … top PGS decile ~75% university attendance vs ~25%" has no link.
  - Severity: medium

- **[F7] session=06e7a264 msg=4aa44873-f22e-4096-b385-495e645f2054 time=2026-05-08 23:56:50 user=bneale backend=perplexity**
  - Question: (same)
  - Claim: "## 9. Summary Table … | Domain | Heritability (SNP h²) | … | Substance Use Disorders | ~50% | *ADH1B*, *CHRNA5*, *DRD2* | ~5–10% |"
  - Source: not persisted; no citation on the table
  - Problem: 1, 3, 7 — ~50% is the twin-heritability of SUDs, not SNP h² (published SNP h² for AUD/CUD/OUD is ~5–15%); the column mixes twin and SNP estimates without saying so and none of the ten rows is sourced.
  - Severity: medium (a table in a "review" the user asked for is exactly what gets copied onward)

- **[F8] session=f28b4c65 msg=4e511fb6-0255-41f7-9907-a8e2d23720f0 time=2026-05-14 14:28:05 user=bneale backend=none**
  - Question: what are the associations to statin onset in finngen
  - Claim: "## PASS 2 — LITERATURE CONTEXT … The top genes are all well-established in lipid biology and cardiovascular genetics." / "PCSK9 inhibitors are now approved drugs." / "**HMGCR** … variants here may reflect pharmacogenomic effects on statin response"
  - Source: no literature call
  - Problem: 7 — the section header promises literature and delivers uncited prose; the HMGCR "pharmacogenomic" reading is speculation presented under a literature heading.
  - Severity: low

- **[F9] session=bf28d688 msg=51be5e16-a4fd-4fbb-8f2c-8fe35abdc5d4 time=2026-05-14 18:09:17 user=bneale backend=perplexity**
  - Question: what can you tell me about KEAP1 associations
  - Claim: "The synonymous variant 19:10489742:G:A (AF=1.9%) shows genome-wide significant association with thyroid disease … This is consistent with the 2025 JCEM paper showing KEAP1 germline mutations cause familial multinodular goiter via NRF2 hyperactivation."
  - Source: not persisted; the assistant's own Pass 2 says "NGS in 39 familial multinodular goiter patients identified 5 KEAP1 germline heterozygous mutations"
  - Problem: 6, 1 — a common synonymous variant (AC ≈ 15,000) and five rare mutations in a 39-family series are not the same evidence class; "consistent with" and the causal "cause" are stronger than 5/39 supports. (The same message otherwise scrutinises well — see G1.)
  - Severity: low

- **[F10] session=05887617 msg=e64e7513-f78b-4e3b-a6a7-bb6496eb7fe7 time=2026-07-29 02:57:44 user=bneale backend=perplexity**
  - Question: Does the newest SCHEMA sequencing data support FURIN loss-of-function as conferring stronger risk to schizophrenia?
  - Claim: "[Hou et al. 2018] **rs4702 G allele creates a miR-338-3p binding site in the FURIN 3′UTR** → allele-specific FURIN downregulation → reduced BDNF maturation/secretion. Mechanism for the risk allele being the low-FURIN allele" … "**Loss of FURIN function is the risk direction in five independent systems plus SCHEMA2 burden.**"
  - Source: "Allele-specific downregulation of FURIN by miR-338-3p was validated with a luciferase reporter assay. Furthermore, we demonstrated that miR-338-3p-mediated FURIN inhibition reduced brain-derived neurotrophic factor (BDNF) maturation and secretion in human embryonic kidney 293T cells."
  - Problem: 1, 2, 8 — the HEK293T cell line and reporter assay are dropped and the result is promoted to "Mechanism"; it is then counted as one of "five independent systems" alongside a zebrafish knockdown, a Drosophila mutant and a review ("[Zhang et al. 2022] Review: FURIN mRNA reduced in SCZ and AD prefrontal cortex"). Same message on the loaded data: "FURIN's 1.46×10⁻⁴ falls ~15-fold short, and PTV-only is p = 0.0337 with a CI (OR 1.11–12.9) that barely excludes 1."
  - Severity: medium

- **[F11] session=05887617 msg=d7f54e76-727d-4820-9944-c58b1fb01e15 time=2026-07-29 02:57:44 user=bneale backend=perplexity**
  - Question: please lookup FURIN - arthrosis/arthritis in Genebass data to square off the story
  - Claim: "[Lin et al. 2018] **The key paper.** … Furin is expressed in healthy cartilage and **reduced in damaged OA cartilage**." … "[Lin et al. 2018] is a direct, quantitative, causal test of the exact prediction: **more furin → less OA** … with furin **depleted in damaged OA cartilage** … So low FURIN → OA risk holds in fine-mapped human genetics, in colocalization, and in a mouse intervention model."
  - Source: "Mice underwent destabilization of the medial meniscus (DMM) … received furin (1 U/mice), α1-PDX (14 µg/mice) or vehicle. … the OA score was lower with furin than vehicle treatment (6.42 ± 0.75 vs 9.16 ± 0.6, p < 0.01) … Conversely, α1-PDX had no effect on OA cartilage structure."
  - Problem: 3, 1 — the abstract contains nothing about furin being reduced/depleted in damaged cartilage (that sentence is not in the source); and the loss-of-function arm the prediction actually needs — the furin inhibitor — was **null** in the source and is omitted. Group sizes are not given by either. The paper is then made the linchpin ("the story does get squared off") after the user had said "concur with your assessment on all points".
  - Severity: high

- **[F12] session=05887617 msg=b9ffe641-4ca3-4ac7-9c24-e119be324e75 time=2026-07-29 03:02:26 user=bneale backend=perplexity**
  - Question: what do you know about the biological regulators of furin?
  - Claim: "| miR-338-3p (replication) | [Yang et al. 2022] — rs4702-A associated with higher FURIN and BDNF in serum/PBMC of 106 glioma patients post-radiotherapy …" and in the summary table "| Post-transcriptional | **miR-338-3p** (rs4702-dependent), miR-24-3p | Strong, allele-specific, replicated |"
  - Source: "We grouped the 106 glioma patients recruited according to the rs4702 polymorphism … rs4702-A was remarkably associated with increased enterotype I and decreased enterotype III in the stool … the presence of A allele … could obstruct the suppressive effect of miR-338-3p upon the expression of FURIN and BDNF in intestinal flora."
  - Problem: 1, 5 — a 106-patient single-cohort study whose abstract attributes a human miRNA effect to "intestinal flora" is graded as the *replication* that makes the mechanism "Strong … replicated". A critical reader would have called it non-confirmatory and kept the Hou 2018 result at "one reporter assay".
  - Severity: medium

- **[F13] session=3d6deffa msg=74c49424-cd23-4abc-a142-90e0eac85ef8 time=2026-07-29 19:32:58 user=bneale backend=none**
  - Question: show me the top end of the schema2 results
  - Claim: "These are **established SCHEMA findings**, not new signals: SETD1A, ZMYM2, RB1CC1, XPO7, SP4, TRIO, CUL1, HERC1 and SCAF1 are the canonical schizophrenia exome genes from the SCHEMA analysis."
  - Source: no literature call
  - Problem: 7, 3 — ZMYM2 and SCAF1 are not among the ten exome-wide genes of Singh et al. 2022 (CACNA1G, GRIA3, GRIN2A are, and are absent here); they are SCHEMA2-wave results. The assistant contradicts itself a month later (2f23256d/44f716f9): "I have not verified whether it was already reported in the published SCHEMA gene list … treat novelty as unchecked".
  - Severity: medium

- **[F14] session=925c7984 msg=cdbfee2a-6be5-4b83-9bbe-37d7066497e5 time=2026-09-23 15:11:21 user=bneale backend=perplexity** (answered by "Claude Opus 5" after "Claude Fable 5.1 declined this request")
  - Question: what is known about GPR17
  - Claim: "Metabolism: 18 nonsynonymous variants found in insulin-resistance/obesity/hypercholesterolaemia cohorts, several with altered cAMP/Ca²⁺/β-arrestin signalling — [Conley et al. 2021, JBC]. That review also concluded there is no replicated common-variant GWAS locus for GPR17"
  - Source: "Here, we analyzed GPR17 coding sequences from control and disease cohorts … We identified 18 nonsynonymous GPR17 variants, including eight variants that were exclusive to the disease cohort. … links between human GPR17 genetic variants, downstream cellular signaling, and metabolic diseases have yet to be reported."
  - Problem: 3, 1 — Conley 2021 is a primary candidate-gene sequencing/functional study, not a review, and it says human variant–disease links are unreported, not that a GWAS locus failed replication; cohort sizes are absent from both. "several with altered … signalling" omits the abstract's own caveat that a synthetic agonist was used because the endogenous ligand is unknown.
  - Severity: low

### The GPR17 "demyelination in schizophrenia, ~5 people" claim (session 925c7984)

The sentence the user remembers is **not in the persisted transcript**. Grep of the session for `demyelin`, `schizophren`, `Nasu`, `strong evidence`, `five` gives:

- Line 4119 (RESULT of call 1, `GPR17 receptor oligodendrocyte differentiation myelination remyelination`), record 2, **Satoh et al. 2017, Intractable Rare Dis Res, PMID 28357182**: "Pathologically, the brains of NHD patients exhibit extensive demyelination designated leukoencephalopathy … **We studied the expression of GPR17 in five NHD brains and eight control brains** by immunohistochemistry … we did not find statistically significant differences in the number of GPR17-expressing cells between NHD and control brains". This is the only five-person study in the session. It is a **null** IHC study of Nasu-Hakola disease (a TREM2/TYROBP leukoencephalopathy that presents with frontal-lobe psychiatric symptoms) — the natural raw material for a summariser to render as "demyelination … psychiatric/schizophrenia-like".
- Line 4121 (RESULT of call 2): the only "schizophrenia" in the session is a Perplexity-native snippet from a 2023 review, about **GPR12** and antipsychotic response ("association between GPR12 and antipsychotic response in schizophrenia treatment") — not GPR17. The call's `summary` field is truncated after 100 characters ("The most directly relevant human papers I found are **2021 functional genetics studies of natu…"), so whatever Perplexity's prose said about schizophrenia is unrecoverable from this file.
- Line 4149 (assistant, persisted answer): the only demyelination statement is "loss of Gpr17 enhances remyelination after lysolecithin demyelination ([Viganò et al. 2018, Sci Rep])" — a mouse model, under a bullet that starts "KO mice"; Viganò 2018 is not among the six visible records (RESULT truncated at record 6 of 8), so its provenance is unverifiable but it is not the complaint.
- Between the two literature calls and the ChEMBL call the transcript records "*[Claude Fable 5.1 declined this request; Claude Opus 5 answered instead]*". Any partial answer Fable 5.1 streamed before declining is not persisted. So the complaint is most likely about (a) that unpersisted draft, or (b) the truncated Perplexity summary text — both built on Satoh 2017 (n = 5 + 8, null result, wrong disease). What *is* persisted (the Opus 5 answer) does not make the claim and correctly frames GPR17 as "a well-studied *biology* gene … but a weak *human genetics* gene".

Actionable: persist the `summary` field in full and persist declined-model drafts; otherwise this class of complaint cannot be audited.

## 3. Counter-examples (baseline good behaviour)

- **[G1] session=bf28d688 msg=51be5e16 time=2026-05-14 18:09:17 backend=perplexity** — "A case-control study found KEAP1 rs1048290 associated with COPD risk (P=0.0015, OR=0.72 …). This is a candidate-gene study, not a GWAS." / "Sanger sequencing in 27 DVT patients … Small study (n=37 total), should be interpreted cautiously." Summary table grades "Weak (candidate gene, not GWAS)", "Moderate (small study, n=39)". Source not persisted.
- **[G2] session=c22d6338 msg=28894dc7 time=2026-05-05 16:11:32 backend=perplexity** — "| Mouse/cell functional studies | **Yes** (protective) | Moderate (animal models) | … | Therapeutic target potential | **Yes** (preclinical) | Preclinical only |" and "The HNF4A–MASH connection is primarily a **molecular biology story**, not a human genetics story".
- **[G3] session=05887617 msg=d7f54e76 time=2026-07-29 02:57:44 backend=perplexity** — on the Nature 2025 OA paper: "⚠️ This came back as a perplexity-sourced snippet with `metadata_source: perplexity` (no PMID/DOI resolved), so it is an unverified quotation from Methods/Supplementary Table 28, not a full read"; and "the mouse OA evidence in Pass 2 comes from [Lin et al.], a pharmacological/DMM study — **not** from a curated germline knockout record." Source matches.
- **[G4] session=05887617 msg=b9ffe641 time=2026-07-29 03:02:26 backend=perplexity** — "The perplexity summary flagged a citation to *Jones et al.* … ⚠️ that primary paper was **not** returned in my results, so I cannot cite it directly" / "⚠️ I searched for **C/EBPβ** and found **no** paper linking it to the FURIN promoter in these results. Do not assume that regulator."
- **[G5] session=05887617 msg=d060e3f4 time=2026-07-29 03:11:42 backend=perplexity** — "[eQTLGen Phase 2] (per search summary) … ⚠️ This came via the perplexity summary without a resolved DOI/PMID of its own — treat the sample size as unverified" / "⚠️ **No FURIN-specific trans-eQTL paper was found.** The search summary states explicitly…"
- **[G6] session=05887617 msg=cafc493d time=2026-07-29 03:04:45 backend=perplexity** — "There is even a [bioRxiv preprint] … ⚠️ That is a **preprint**, not peer-reviewed, and I found **no evidence of any direct ZMYM2–FURIN regulatory relationship**. Convergence on the same disease area is not a molecular connection".
- **[G7] session=05887617 msg=6dd57b1c time=2026-07-29 19:47:00 backend=perplexity** — "⚠️ **The ADAM23 evidence is weaker than LRP1's.** 'Furin *or a related enzyme*' leaves open PACE4/PC5/PC7" and "**Observing 2 is entirely consistent with chance.** I did **not** run a formal enrichment test, and with n = 2 no such test would be meaningful." Source matches ("maturation depends on cleavage by furin or a related enzyme").
- **[G8] session=05887617 msg=e64e7513 time=2026-07-29 02:57:44 backend=perplexity** — "Anyone claiming SCHEMA 'establishes' FURIN LoF as a SCZ risk factor is overreading." / "Note that *Furin*-null mice are embryonic lethal per the general literature, but I have not verified that with a tool here and am not asserting it as data."
- **[G9] session=3d6deffa msg=3c6fedb3 time=2026-07-29 19:38:19 backend=perplexity** — "[Fan 2013] found faster FAK focal-adhesion disassembly in patient-derived progenitors (n = 9 vs 9 — small, and a cellular-phenotype study, not a genetic one)". Source: "olfactory mucosa of schizophrenia patients …" (n visible in the assistant's table row: "9 schizophrenia patients vs 9 controls").
- **[G10] session=05059394 msg=67ac13cf time=2026-07-29 19:49:07 backend=perplexity** — "[Babini et al. 2024] **rs13376333 is the top 1q21 AF SNP and its electrophysiological effect is still unknown** — direction (GoF vs LoF) unresolved"; "the tested cell lines are K562/HepG2/SK-N-SH/HCT116/A549 — **no cardiomyocyte line**, so absence of an emVar call here is weak evidence."
- **[G11] session=429ed55f msg=987de233 time=2026-08-04 17:35:08 backend=perplexity** — "candidate-gene studies report IL1RL1 rs13015714/rs2058660 in CD in a single Italian cohort of 805 CD/816 UC/752 controls ([Latiano et al. 2013]) — small, unreplicated, and not recovered by the million-sample meta-analysis above." Source not persisted.
- **[G12] session=3a7a000d msgs=18842126 / b0a6cb76 time=2026-08-10 15:34–15:39 backend=perplexity** — "[Ren et al. 2018] reports elevated FGF5 mRNA/protein in hypertensives, but n=34 vs 25 — too small to weigh heavily." Next turn weighs Park 2014 siRNA against Liu 2017 CRISPR-KO rats ("It was directly tested and **failed**") and retracts its own earlier data claim: "**First, a correction to my previous answer.** … That was wrong." Liu 2017 source matches.
- **[G13] session=8ccfb380 msg=3c5abeeb time=2026-08-13 03:25:31 backend=perplexity** — "The association traces to small Danish/Scottish/UK candidate-gene studies — [Severinsen et al. 2006] (103 SCZ, 162 BPD, 200 controls) and [Nyegaard et al. 2010] (~1,000 cases) — sample sizes 2–3 orders of magnitude below current GWAS, unreplicated at genome-wide standards." Source not persisted.
- **[G14] session=2f23256d msgs=6b72057e / 44f716f9 time=2026-08-26 backend=perplexity** — "A review notes only a single, unreplicated link to neural tube defects ([Almannai et al. 2022]) — weak evidence." / ZMYM2: "the perplexity literature backend returned no primary ZMYM2–schizophrenia genetic paper, so treat novelty as unchecked rather than established."
- **[G15] session=17201e6a msg=3db3e4e7 time=2026-07-29 21:24:58 backend=none** — "I did not run a literature search for this turn: the question is an inventory of what data exists, and every statement above is read directly from `list_datasets`, `search_phenotypes`, and the credible-set / gene-burden tables." Also f1b3a5a3/a1334ea5: "This is a hypothesised link to lithium pharmacology, not yet shown to predict treatment response."

## 4. Patterns

1. **The backend is not the main culprit — the model's synthesis step is.** All 37 calls were Perplexity, yet in the persisted RESULTs 87/115 records are structured Europe PMC entries with PMIDs and honest abstracts. The three worst findings with a visible source (F10, F11, F12) are cases where the abstract *says* HEK293T / luciferase / "α1-PDX had no effect" / "106 glioma patients … intestinal flora" and the assistant's paraphrase drops it and promotes the result to "Mechanism", "causal test", "replicated". The genuine Perplexity hazard is the 28 `metadata_source: perplexity` snippets and the free-text `summary` (no PMID, sentence fragments); the model flags these well in the FURIN sessions (G3, G5) and relays them silently elsewhere (F6 press release, F14, the KCNN3 "Perplexity summary also cites…").

2. **Two-tier scrutiny, split by paper type rather than by data-vs-literature.** When a paper is a *genetics* paper, the genetics rubric transfers intact: candidate-gene n's (G1, G11, G13), pseudo-credible-set caveats, exome-wide thresholds, "unreplicated". When a paper is *functional* (cell line, in vivo model, IHC series, review), there is no rubric at all: no model system named, no group size, no null arm, no primary-vs-review distinction (F2, F9, F10, F11, F12, F14). The GPR17 complaint sits exactly in this gap — GPR17 is a pure functional-biology gene, and the only candidate source is a five-brain null IHC study in a different disease.

3. **Memory-sourced literature is where the factual errors are.** F4 (DISC1/DTNBP1/NRG1/COMT), F7 (SNP h² ~50% for SUD), F13 (ZMYM2/SCAF1 as "canonical SCHEMA"), F5 (~5×) all come from messages with no tool call or from uncited table cells. The "PASS 2 — LITERATURE" template invites filling with prose when no search ran (F1, F8). Requiring every literature sentence to trace to a record ID would remove this class outright.

4. **Provenance discipline improved sharply from late July**: "Backend queried: perplexity", "⚠️ unverified snippet", explicit "not persisted/not returned" statements, and "I did not run a literature search" are all post-2026-07-29. But the June statement "All searches were conducted using both the Perplexity and Europe PMC backends" (F6) is simply false, and the persistence layer still truncates the `summary` field and long RESULT lists, so ~24 citations per this file cannot be audited at all.

5. **Evidence-tier mismatch is real but asymmetric in a specific way.** The assistant never over-hedges the loaded data and never under-hedges a *genetics* paper; it under-hedges functional papers precisely when they *agree* with the loaded data (F10, F11: "Every functional study points the same way", "the story does get squared off"). Confirmation is being read as convergence. A concrete fix: a literature-evidence rubric mirroring the genetics one — study type (primary/review/preprint), model system (human/mouse/fly/cell line, which line), n or group size, replication status, null arms — emitted per cited paper, and a rule that a review or a Perplexity summary may point to evidence but is never itself the evidence.

## 5. Borderline cases skipped (not counted as findings)

Citations present in assistant text whose matching record falls beyond the truncation point of the RESULT list (so presence can be neither confirmed nor denied): Chick 2025 (e64e7513); Laprise 2002, Riby 2008, Anderson 1997/2002, Feliciangeli 2006, Vey 1994, Evers 2024 (b9ffe641); Connaughton 2020 (cafc493d); Herz 1990, Seidah 2013, Longpré & Leduc 2004 (6dd57b1c); Zachary 1997, Zhang 2022, Naser 2018, Huang 2025 (3c6fedb3); Diness 2015, Adelman 2016, Kalstø 2019, Lozano-Velasco 2020 (67ac13cf); McAfee 2023 (442105d9); Liu 2011 (b0a6cb76); Radhakrishnan 2014, Sekine & Motohashi 2021 (923817f3); Thorgeirsson 2025 (a1334ea5); Viganò 2018 (cdbfee2a). One more skipped: CHRM4/442105d9 attributes to Rietschel 2012 "genome-wide significance in combined samples with independent replication" and the AMBRA1/DGKZ/CHRM4/MDK naming, while the persisted snippet only shows "1169 … SCZ patients … 3714 ethnically matched controls" — likely true from the full paper, unverifiable here. Two genetics-side overclaims noted but out of scope: "PIP = 1.0 … essentially certain to be causal" (e0bf5d26) and the initial ANTXR2 credible-set misattribution (3a7a000d, self-corrected).
