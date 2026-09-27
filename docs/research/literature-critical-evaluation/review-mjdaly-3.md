# Literature-scrutiny review: mjdaly-3.txt

Scope: 9 sessions (2026-07-25 to 2026-09-13), 24 assistant messages, user=mjdaly, `lit_backend=perplexity` on every message. Every RESULT block in this transcript is display-truncated (`...]]`), so "not present in RESULT" below means *not visible in the persisted portion*; only cases where the visible RESULT actively contradicts the assistant's citation are reported as verified provenance errors.

## 1. Counts

| Item | Count |
|---|---|
| `search_scientific_literature` calls, total | 23 |
| — with persisted RESULT (backend field `perplexity`, records carry `metadata_source: europepmc` for ~85%) | 15 |
| — older format, no RESULT persisted (assistant text states backend `perplexity`) | 8 |
| — backend `europepmc` | 0 |
| `web_search` calls | 1 (brensocatib FDA approval, msg 4a1b35bc) |
| Assistant messages that report literature | 18 of 24 |
| Messages citing literature with **no** literature tool call in that message | 2 (bffda78b: "textbook SCZ fine-mapping exemplars from Trubetskoy et al. 2022", no PMID; d5a8bc0c: SLC39A8 pleiotropy "already well documented", no citation at all) |
| Messages blending from-memory metadata into a search-backed table (cat. 7) | 5 (454d6aea, c9cb2ae8, 6bb7b174, 67a81f33, 5136c9a5) |
| Findings, category 1 (face-value pass-through) | 8 |
| Findings, category 2 (evidence-tier mismatch) | 2 |
| Findings, category 3 (source does not support claim) | 4 |
| Findings, category 4 (Perplexity summary as source) | 2 |
| Findings, category 5 (old candidate-gene / small-n as support) | 1 |
| Findings, category 6 (unreconciled contradiction) | 0 |
| Findings, category 7 (missing/blended provenance) | 6 |
| Findings, category 8 (review/consensus as evidence) | 5 |
| Distinct findings (some carry two categories) | 21 (0 high, 3 medium, 18 low) |
| Counter-examples | 14 |
| User pushback on a literature claim | 0 (user replies were all follow-ups; the one rating=3 comment concerns FINEMAP data access, not literature) |
| Borderline cases skipped | 6 (listed at end of §2) |

## 2. Findings

- **[F1] session=ef50921d msg=c9cb2ae8-5736-4558-82f0-77045e376b17 time=2026-07-31 19:25 user=mjdaly backend=perplexity**
  - Question: what about ADAMTS7 — are there any convincing coding variants in credible sets for any phenotype?
  - Claim: "[MacDonald et al. 2022, Mol Cell Proteomics] | TAILS proteomics | States plainly that **loss-of-function mutations in ADAMTS7 are associated with protection from CAD**" … "these alleles are **protective**, matching the loss-of-function-is-protective framing in [MacDonald et al. 2022] and [Mizoguchi et al. 2021]"
  - Source: "Loss-of-function mutations in the secreted enzyme ADAMTS7 (a disintegrin and metalloproteinase with thrombospondin motifs 7) are associated with protection for coronary artery disease. ADAMTS7 catalytic inhibition has been proposed as a therapeutic strategy"
  - Problem: 8, 1 — an introductory framing sentence of a substrate-discovery paper is relayed as the paper's finding and used as the human-genetic prior; no human LoF-burden study exists (the assistant itself says in point 6 that the LoF-protective hypothesis "is untested in the burden data available to me"). A critical reader would say the "LoF is protective" claim rests on the S214P common-missense mechanism, not on LoF.
  - Severity: low

- **[F2] session=ef50921d msg=c9cb2ae8-5736-4558-82f0-77045e376b17 time=2026-07-31 19:25 user=mjdaly backend=perplexity**
  - Question: same as F1
  - Claim: "[Mizoguchi et al. 2021, Circ Res] | Mouse genetics | 'Coronary Disease Association With ADAMTS7 Is Due to **Protease Activity**' — proatherogenicity requires the catalytic function" … "[Mizoguchi et al. 2021] showed the CAD relevance is specifically **protease activity**"
  - Source: "title": "Coronary Disease Association With ADAMTS7 Is Due to Protease Activity.", … "abstract": "[Figure: see text]."
  - Problem: 7, 3 — the persisted record contains only a title; "Mouse genetics", "proatherogenicity requires the catalytic function" and "showed" are supplied from memory and presented as read from the paper.
  - Severity: low

- **[F3] session=ef50921d msg=c9cb2ae8-5736-4558-82f0-77045e376b17 time=2026-07-31 19:25 user=mjdaly backend=perplexity**
  - Question: same as F1
  - Claim: "[Chen et al. 2019, Aging] | 1,279 IS cases / 1,268 controls, 2 cohorts | rs3825807, rs11634042, rs7173743 associated with **lower ischaemic stroke** risk, specifically large-artery atherosclerosis subtype"
  - Source: "Four variant genotypes of the ADAMTS7 gene were identified using the Multiplex SNaPshot assay. The rs3825807, rs11634042, and rs7173743 variants of ADAMTS7 were related to lower IS risk in both initial and replication cohort … Our findings confirm the role of ADAMTS7 in the pathophysiology of IS"
  - Problem: 5 — a 4-SNP candidate-gene SNaPshot study is tabulated as an association result while the two smaller studies in the same table (Iwanicka, Bayoglu) get "underpowered"/"unreplicated" flags; the candidate-gene design and the paper's "confirm the role" overreach are not noted.
  - Severity: low

- **[F4] session=ef50921d msg=65cb88e1-3653-4fbd-8eaa-94d2ea8cce97 time=2026-07-31 19:52 user=mjdaly backend=perplexity**
  - Question: Could you explore MMP2 and MMP14 in the same way
  - Claim: "[Ng et al. 2017, Am J Hum Genet] | … | **rs1042704 in MMP14** identified as the likely causal coding variant at its locus — the only exonic protein-coding variant in that signal"
  - Source (Ng abstract): "We validated association at all nine previously described signals and discovered 17 additional variants with p ≤ 5 × 10-8" (no mention of rs1042704 or MMP14); the only link is a medRxiv snippet: "(rs1042704, rs2306022 and rs11672517) reported by Ng et al. in the most recent GWAS. Two of these are non-synonymous changes in the MMP14 and ITGA11 genes."
  - Problem: 3 — "identified as the likely causal coding variant" and "the only exonic protein-coding variant in that signal" are not supported by anything retrieved; attributed to Ng via a second-hand preprint sentence.
  - Severity: low

- **[F5] session=ef50921d msg=65cb88e1-3653-4fbd-8eaa-94d2ea8cce97 time=2026-07-31 19:52 user=mjdaly backend=perplexity**
  - Question: same as F4
  - Claim: "**Direct functional assay:** the [bioRxiv preprint] reports D273N MT1-MMP retains only **17% of cell-surface collagenolytic activity** — a large, measured protein-level defect." … bottom line: "…and measured protein-level functional defects"
  - Source: "title": "An SNP variant MT1-MMP with a defect in its collagenolytic ...", "authors": "", "year": "", "abstract": "…the variant MT1-MMP (MT1-N273) exhibits only 17% of \ncell surface collagenolytic activity ...", "is_preprint": true, url biorxiv 2020.06.09.142513v1
  - Problem: 2, 1 — the table correctly labels it "(bioRxiv preprint)", but PASS 3 promotes a 2020 unreviewed preprint snippet (no cell system, no replicate count, no authors resolved) into one of "Three independent readouts converge" and the bottom line's "measured protein-level functional defects". Contrast the same message on the loaded data: "⚠️ `finngen_ukbb` … **pseudo credible sets** whose PIP of 0.99 is a heuristic artefact … The FinnGen R14 numbers are the ones to rely on."
  - Severity: medium

- **[F6] session=ef50921d msg=6bb7b174-82da-49bc-8254-5ce7f23c97d9 time=2026-07-31 20:13 user=mjdaly backend=perplexity**
  - Question: systematic report on BMP1 — Mendelian associations and credible sets with coding/QTL evidence
  - Claim: "[Takanosu et al. 2026, J Vet Diagn Invest] | Feline genetics | Homozygous missense in the **zinc-dependent metalloprotease domain** of BMP1 in a cat with multiple fractures but normal bone morphology — cross-species support for the catalytic domain"
  - Source: "A 3-mo-old domestic shorthair cat was presented with multiple fractures … Whole-genome sequencing identified a homozygous missense mutation … In silico analysis predicted … Our findings suggest that a missense mutation in BMP1 may cause feline osteogenesis imperfecta."
  - Problem: 1 — a single-animal case report with in-silico-only support is labelled "cross-species support"; the paper itself says "may cause". No n, no segregation, no functional assay.
  - Severity: low

- **[F7] session=ef50921d msg=6bb7b174-82da-49bc-8254-5ce7f23c97d9 time=2026-07-31 20:13 user=mjdaly backend=perplexity**
  - Question: same as F6
  - Claim: "[Jovanovic & Marini 2024, Calcif Tissue Int] | Review | Places BMP1 in the 'collagen-related disorder' paradigm … BMP1 = OI type XIII, often with bone hypermineralization by qBEI"
  - Source: abstract discusses "recessive mutations in TENT5A, MESD, KDELR2 and CCDC134 whose causality of OI types XIX, XX, XXI and XXI" — BMP1 is not mentioned anywhere in the retrieved record.
  - Problem: 7, 3 — "BMP1 = OI type XIII, often with bone hypermineralization by qBEI" is a from-memory detail attached to a citation whose retrieved content does not contain it.
  - Severity: low

- **[F8] session=ef50921d msg=a977eeba-95ba-436d-9b97-b7df273997eb time=2026-07-31 21:07 user=mjdaly backend=perplexity**
  - Question: systematic report on GPR37 (Mendelian, credible sets/QTL, Genebass)
  - Claim: "[Leinartaitė & Svenningsson 2017, Trends Pharmacol Sci] | Review | … Misfolded GPR37 drives ER stress and dopaminergic death and forms a **core structure of Lewy bodies**" — restated next turn (67a81f33) as "a **Lewy body core component**" within "The mechanistic case for GPR37 in PD is substantial"
  - Source: not visible in persisted RESULT (record truncated); the review is the cited source.
  - Problem: 8 — a review's summary of a small post-mortem immunohistochemistry literature is relayed as an established property of the protein, twice, without pointing at the primary study or its n.
  - Severity: low

- **[F9] session=ef50921d msg=67a81f33-2f59-4273-9f7c-716242e56ec9 time=2026-07-31 21:12 user=mjdaly backend=perplexity**
  - Question: Is there any evidence for the GPR37 locus from the PGC GWAS or the GP2 Parkinson's GWAS?
  - Claim: "and — most pointedly — **CSF ecto-GPR37 elevated in PD patients** ([Argerich et al. 2024]) … the CSF biomarker signal is more plausibly a *downstream consequence* of neurodegeneration" (previous turn: "**CSF ecto-GPR37 is increased in PD**; elevated in slow- but not rapid-progression patients")
  - Source: Argerich 2024 not visible in either persisted RESULT (both truncated).
  - Problem: 1, 7 — the biomarker finding is relayed twice with no cohort size, no replication, no effect size, then treated as the hypothesis the negative GP2 result "lands on". Contrast: every GP2 p-value in the same message is quoted with AF, beta, se and a Finnish-enrichment power caveat.
  - Severity: low

- **[F10] session=ef50921d msg=67a81f33-2f59-4273-9f7c-716242e56ec9 time=2026-07-31 21:12 user=mjdaly backend=perplexity**
  - Question: same as F9
  - Claim: "[Satake et al. 2009, Nat Genet] | PD GWAS, Japanese, 2,011 cases / 18,381 controls | Identified PARK16, BST1, SNCA, LRRK2. **No GPR37**" and "[IPDGC 2009, Nat Genet] | PD GWAS, European | PARK16, SNCA, LRRK2, MAPT, BST1. **No GPR37**"
  - Source: the only persisted trace is a reference-list fragment inside a medRxiv snippet: "Tsunoda, T., Watanabe, M., Takeda, A., et al.\n(2009).\nGenome-wide association study\n404\nidentifies common variants at four loci"; IPDGC 2009 appears in no visible record.
  - Problem: 7 — sample sizes, gene lists and the "No GPR37" verdicts are from memory but sit in the "PASS 2 — LITERATURE / Backend queried: perplexity" table alongside search-derived rows; the reader cannot separate them.
  - Severity: low

- **[F11] session=ef50921d msg=67a81f33-2f59-4273-9f7c-716242e56ec9 time=2026-07-31 21:12 user=mjdaly backend=perplexity**
  - Question: same as F9
  - Claim: "[Langston et al. 2022, Sci Transl Med](https://pmc.ncbi.nlm.nih.gov/articles/PMC9809150/)"
  - Source: "title": "Association of a common genetic variant with Parkinson's disease is mediated by microglia.", … "pmid": "35895835", "pmcid": null, … "url": "https://pubmed.ncbi.nlm.nih.gov/35895835/"
  - Problem: 7 — the retrieved record has no PMC id; the assistant substituted a PMC link not present in the result. Verified against the persisted RESULT.
  - Severity: low

- **[F12] session=ef50921d msg=67a81f33-2f59-4273-9f7c-716242e56ec9 time=2026-07-31 21:12 user=mjdaly backend=perplexity**
  - Question: same as F9
  - Claim: "The perplexity summary states directly that GPR37 'is **not established as a genome-wide significant Parkinson's disease GWAS locus**,' and quotes a source saying *'no mutations or risk variants in the Pael-R/GPR37 gene have been described in PD.'*"
  - Source: no summary text is present in the persisted RESULT; the quoted "source" is unidentified.
  - Problem: 4 — AI-summary text relayed as corroboration with an unnamed inner citation. Mitigated: it is labelled as the Perplexity summary and the conclusion rests on the loaded GP2 statistics.
  - Severity: low

- **[F13] session=d1681f3a msg=2339977b-f400-41f8-a416-9c95d5979b2f time=2026-07-29 02:47 user=mjdaly backend=perplexity**
  - Question: Does the newest SCHEMA sequencing data support FURIN loss-of-function as conferring stronger risk to schizophrenia?
  - Claim: "**Convergence across model systems.** Every functional study points the same way … **Loss of FURIN function is the risk direction in five independent systems plus SCHEMA2 burden.**" and "[Hou et al. 2018] | **rs4702 G allele creates a miR-338-3p binding site** … Mechanism for the risk allele being the low-FURIN allele"
  - Source (Hou): "Allele-specific downregulation of FURIN by miR-338-3p was validated with a luciferase reporter assay. Furthermore, we demonstrated that miR-338-3p-mediated FURIN inhibition reduced brain-derived neurotrophic factor (BDNF) maturation and secretion in human embryonic kidney 293T cells." Source (Foka): "loss of furin1, the Drosophila homolog … results in defective habituation to recurrent footshocks"
  - Problem: 2, 1 — the genetics gets "p = 1.46×10⁻⁴ falls ~15-fold short … suggestive rather than conclusive", but a HEK293T luciferase assay, a Drosophila habituation assay, a URL-only Nature Genetics snippet and a review are *counted* as five independent confirmations rather than weighted; none is given n, replication, or the cell-system caveat in the analysis text. "Mechanism for the risk allele" from a kidney-cell reporter is stated without qualification.
  - Severity: medium

- **[F14] session=d1681f3a msg=17ad23b7-c46f-4dbc-a465-5a9173d87f6f time=2026-07-29 02:54 user=mjdaly backend=perplexity**
  - Question: please lookup FURIN - arthrosis/arthritis in Genebass data to square off the story
  - Claim: "[Lin et al. 2018, Sci Rep] | **The key paper.** … In mouse DMM-induced OA, furin treatment **lowered OA score to 6.42±0.75 vs 9.16±0.6 vehicle (p<0.01)** … **The story does get squared off** — from a different direction … a direct, quantitative, causal test of the exact prediction"
  - Source: "Mice underwent destabilization of the medial meniscus (DMM) to induce OA, then received furin (1 U/mice), α1-PDX (14 µg/mice) or vehicle. In mice with DMM, the OA score was lower with furin than vehicle treatment (6.42 ± 0.75 vs 9.16 ± 0.6, p < 0.01)"
  - Problem: 1 — one exogenous-protein injection study (group n not stated, single lab, no replication) is elevated to "the key paper" and "causal test" that squares the story, while the null Genebass burden in the same message is examined for censoring, power and case counts at length. The assistant does note it is "a pharmacological/DMM study — not from a curated germline knockout record".
  - Severity: low

- **[F15] session=d1681f3a msg=5136c9a5-336f-4783-a7d9-9800e545bb15 time=2026-07-30 19:34 user=mjdaly backend=perplexity**
  - Question: are there targets activated/deactivated by FURIN with evidence of relevance to schizophrenia in PGC GWAS or SCHEMA?
  - Claim: "I did not assert substrates from memory. Two sources: … **Literature (brain-focused)** — the [Zhang et al. 2022 review] lists brain furin substrates: BDNF, NGF, MMPs, ADAMs, BACE1, Notch receptor, **LRP1**, GPR37, sortilin (SORT1), BRI2 (ITM2B), Ac45."
  - Source (Zhang abstract, the only retrieved content): "In the brain, the substrates of furin include the proproteins of growth factors, receptors and enzymes." — no gene list.
  - Problem: 4, 8 — the 11-gene substrate roster that drives the whole 29-gene burden scan is attributed to a review whose retrieved abstract contains no list; it came from the AI summary or memory. Questionable members (GPR37, ITM2B) are then tested as "substrates" with equal standing to UniProt-curated TGFB1/NPPB.
  - Severity: low

- **[F16] session=d1681f3a msg=5136c9a5-336f-4783-a7d9-9800e545bb15 time=2026-07-30 19:34 user=mjdaly backend=perplexity**
  - Question: same as F15
  - Claim: "**LRP1 is the answer, and the direction is the reason it's interesting.** … Furin **cleaves LRP1 in neurons** ([Sci Rep 2019]). If FURIN activates LRP1, then losing FURIN should phenocopy losing LRP1 — and both do increase risk. **The signs agree with the mechanism.**"
  - Source: "abstract": "Furin-mediated cleavage of LRP1 and increase in ICD of LRP1 after cerebral ischemia and after exposure of cultured neurons to NMDA", authors/pmid absent (URL-only Perplexity snippet).
  - Problem: 3 — cleavage generating an intracellular domain under ischemia/NMDA is not evidence that furin *activates* LRP1; the headline asserts sign concordance with a mechanism whose direction is only hypothesised ("If FURIN activates"). The conditional is present but the headline is not conditional.
  - Severity: low

- **[F17] session=990622a5 msg=4a1b35bc-2d3d-4420-8abb-3c1251960af0 time=2026-08-03 17:50 user=mjdaly backend=perplexity**
  - Question: how about CTSC and CTSD?
  - Claim: "[Chen et al. 2024, Eur J Med Chem]: CTSC inhibitor B22 shows efficacy in a mouse IBD model — CTSC is already being pursued as an IBD target."
  - Source: not visible in persisted RESULT (6 of 10 records visible; none is Chen 2024).
  - Problem: 1 — a medicinal-chemistry paper's mouse-model claim relayed with no model, n, or effect, and generalised to "already being pursued as an IBD target".
  - Severity: low

- **[F18] session=990622a5 msg=e75b5608-347b-4a5a-ac25-5b1be2f92efd time=2026-08-03 17:45 user=mjdaly backend=perplexity**
  - Question: does the gene CTSB have any known connection to Inflammatory Bowel Disease
  - Claim: "[Wang et al. 2024 MR study]: no forward causal effect of any cathepsin on IBD; reverse direction suggested CD lowers cathepsin B levels — consistent with CTSB being a downstream marker." and "[Menzel et al. 2006]: … cathepsin inhibition ameliorated DSS colitis in mice."
  - Source: not persisted (older message).
  - Problem: 1, 8 — a two-sample MR result (instrument strength, pleiotropy and pQTL platform unstated) is relayed as establishing direction of causation, and a 2006 DSS-mouse inhibitor result is relayed with no n; both feed the bottom line ("plausible *downstream* inflammatory effector").
  - Severity: low

- **[F19] session=3a212b90 msg=1b3e48f1-aa0a-4a3f-aab1-06bbbf591116 time=2026-08-04 16:02 user=mjdaly backend=perplexity**
  - Question: is there any evidence of involvement of COL6A3 in cardiovascular disease
  - Claim: "[Yoshiji et al. 2025, *Nature Genetics*] — proteome-wide MR + colocalization prioritized **COL6A3** as a mediator of obesity → coronary artery disease … This is the strongest existing genetic case" and "[Collagen VI microfibril structure, *Nat Commun* 2025] — notes a COL6A3 variant as a reported risk locus for thoracic aortic aneurysm and dissection, consistent with the FinnGen/MVP/UKBB signal above."
  - Source: not persisted (older message).
  - Problem: 1, 8 — an MR-mediation claim is relayed with no effect size, instrument count or coloc PP; a structural-biology paper's passing "reported risk locus" note (itself a citation of a GWAS) is presented as independent corroboration of the loaded aneurysm signal, which is likely the same or an overlapping GWAS — circular support not flagged.
  - Severity: medium

- **[F20] session=c4489444 msg=da3a452a-1057-42d3-9c51-ef1ed2806689 time=2026-08-03 18:37 user=mjdaly backend=perplexity**
  - Question: does ADAMTS15 link to schizophrenia - and could it connect to FURIN?
  - Claim: "This is well-trodden ground: the risk G allele creates a miR-338-3p site, lowering FURIN and mature BDNF ([Hou et al. 2018]), and CRISPR-edited rs4702 GG neurons show reduced FURIN with pre/postsynaptic deficits ([Schrode et al. 2019, Nat Genet])." and "PNN/ECM disruption is a recurring schizophrenia finding ([Pantazopoulos et al. 2021])"
  - Source: not persisted (older message). Hou's design (HEK293T luciferase) is known from the persisted record in msg 2339977b.
  - Problem: 1, 8 — the HEK293T reporter result is stated as the in-vivo mechanism ("well-trodden ground"); a post-mortem review is cited for "a recurring schizophrenia finding" with no primary study.
  - Severity: low

- **[F21] session=ef50921d msg=454d6aea-a083-4979-90f9-980c1cfacb4b time=2026-07-25 16:00 user=mjdaly backend=perplexity**
  - Question: search all GWAS data with credible sets to see if any coding variants in CTSK are in any phenotypic association
  - Claim: "Note also that CTSK is an **already-drugged target** — odanacatib, a cathepsin K inhibitor, reached Phase III for osteoporosis before discontinuation."
  - Source: not persisted; no citation given.
  - Problem: 7 — from-memory drug-development claim inside a section headed "PASS 2 — LITERATURE / Backend queried: perplexity", with no marker that it did not come from the search.
  - Severity: low

Borderline cases skipped (6): Hosseini 2020 meta-analysis details and Chick 2025 SCHEMA details (records not visible in the truncated RESULT, so neither the claim nor its absence can be checked); Ishaq 2023 "Finnish founder mutation" attribution (abstract cut off mid-sentence in the RESULT); Pei 2019 / He 2023 "BMP1 not reported among leads" (inference from abstracts naming three genes — weak but not wrong); Foka 2022 PMC id change between turns (later RESULT truncated, could be legitimate); Hirano 2025 "new since 2025 — in humans" in the ADAM23 headline (caveated as "one year old and single-family" later in the same message).

## 3. Counter-examples

- **[G1] session=ef50921d msg=454d6aea-a083-4979-90f9-980c1cfacb4b time=2026-07-25 16:00 user=mjdaly backend=perplexity**
  - Question: CTSK coding variants in credible sets
  - Claim: "[Giraudeau et al. 2004, JBMR] | Candidate-gene, ~3,000 perimenopausal Scottish women | … Common CTSK SNPs and STRs **not associated**" … "[Yuan et al. 2023] | Pharmacogenetic, 460 women on alendronate | … **not significant after Bonferroni correction**"
  - Source: not persisted
  - Why good: every literature row carries design and n; null candidate-gene results are treated as nulls and reconciled with the loaded null ("consistent with, not contradicted by, the literature").

- **[G2] session=ef50921d msg=c9cb2ae8-5736-4558-82f0-77045e376b17 time=2026-07-31 19:25 user=mjdaly backend=perplexity**
  - Question: ADAMTS7 coding variants
  - Claim: "[Iwanicka et al. 2024] | 231 cases / 240 controls, Poland | rs3825807 A allele OR = 1.64, p = 0.04. **Small, borderline, unreplicated** — weak evidence" … "**The literature's headline coding variant is conspicuously *not* what fine-mapping picks.** … This is a real discordance between fine-mapping and the mechanistic literature."
  - Source: "genotyping … 115 PAD patients and 116 healthy controls … There was no significant difference in the frequencies of rs1994016 and rs3825807"
  - Why good: small candidate-gene studies down-weighted explicitly; literature-vs-loaded-data contradiction (S214P mechanism vs PIP 0.0534) named and left open rather than resolved by fiat (category 6 handled correctly).

- **[G3] session=ef50921d msg=65cb88e1-3653-4fbd-8eaa-94d2ea8cce97 time=2026-07-31 19:52 user=mjdaly backend=perplexity**
  - Question: MMP2/MMP14
  - Claim: "[Samulenas et al. 2022, Genes] | Candidate-gene, 116 DC cases / 103 controls | … **Small and underpowered**; the reported '14-fold risk' … should be treated as exploratory" and "`get_gene_disease_associations` returned **'No Mendelian disease associations found'** … a gap in the ClinGen/GENCC data as served here, since the literature below documents both genes as established recessive skeletal-dysplasia genes."
  - Source: "we enrolled 219 genomic DNA samples, which were extracted from 116 patients with DC and 103 healthy controls"
  - Why good: candidate-gene n flagged; preprints labelled "(bioRxiv preprint)" / "(medRxiv preprint)" in the table; a tool null contradicted by literature is flagged as a tool gap rather than accepted.

- **[G4] session=ef50921d msg=6bb7b174-82da-49bc-8254-5ce7f23c97d9 time=2026-07-31 20:13 user=mjdaly backend=perplexity**
  - Question: BMP1 report
  - Claim: "I found **no paper in this search linking BMP1 to coronary artery disease** … Two readings are possible: this is an under-recognized locus assignment, or the chr8p21 CAD signal has been attributed elsewhere in the CAD GWAS literature and my search simply did not surface it. I have not verified novelty and cannot claim it."
  - Source: n/a (absence)
  - Why good: absence of a paper in a Perplexity search is treated as a property of the search, not of the literature.

- **[G5] session=ef50921d msg=e318ce78-7ce3-4de7-b80b-7a001962013d time=2026-07-31 20:37 user=mjdaly backend=perplexity**
  - Question: rare coding BMP1 signal in Genebass
  - Claim: "Every published BMP1 OI family makes the same point clinically — heterozygous parents are **phenotypically normal** in [Xu et al. 2019], [Xi et al. 2021] … Genebass burden tests are **heterozygote-dominated additive tests**; for a strictly recessive gene with unaffected carriers, they are testing the wrong genetic model."
  - Source: "The parents were heterozygous carriers for the two mutations respectively, but with normal phenotype."
  - Why good: literature used to explain a null in the loaded data with the correct genetic model, quoting the primary sentence.

- **[G6] session=ef50921d msg=a977eeba-95ba-436d-9b97-b7df273997eb time=2026-07-31 21:07 user=mjdaly backend=perplexity**
  - Question: GPR37 report
  - Claim: "**Fujita-Jimbo et al. 2012, Neurosci Res** — reported in the perplexity summary | Candidate-gene | … ⚠️ I could not retrieve a structured record with a URL, PMID or sample size for this paper from the search result, so I cannot assess its size, replication status, or whether it constitutes Mendelian evidence. Treat as unverified"
  - Source: not in persisted records
  - Why good: exactly the rubric's desired behaviour on a Perplexity-summary-only citation; also "Imai, Soda et al., PNAS (foundational, ~2001) — described in [Leinartaitė & Svenningsson 2017]" labels a second-hand citation as second-hand, and the APOE trans-pQTL is barred from being read as GPR37→Alzheimer's evidence.

- **[G7] session=d1681f3a msg=2339977b-f400-41f8-a416-9c95d5979b2f time=2026-07-29 02:47 user=mjdaly backend=perplexity**
  - Question: SCHEMA and FURIN LoF
  - Claim: "Note that *Furin*-null mice are embryonic lethal per the general literature, but I have not verified that with a tool here and am not asserting it as data." … "Anyone claiming SCHEMA 'establishes' FURIN LoF as a SCZ risk factor is overreading."
  - Source: MGI 503 errors ×3
  - Why good: memory-derived claim explicitly quarantined; exome-wide threshold from the primary paper applied against the loaded burden result.

- **[G8] session=d1681f3a msg=17ad23b7-c46f-4dbc-a465-5a9173d87f6f time=2026-07-29 02:54 user=mjdaly backend=perplexity**
  - Question: FURIN arthrosis in Genebass
  - Claim: "⚠️ This came back as a perplexity-sourced snippet with `metadata_source: perplexity` (no PMID/DOI resolved), so it is an unverified quotation from Methods/Supplementary Table 28, not a full read" … "But their lead is **rs1894401, not rs4702**, and rs1894401 is only at **r² = 0.545** with rs4702 … I would not claim it confirms rs4702"
  - Source: "abstract": "Furin plasma protein quantitative trait loci (pQTLs) colocalize with osteoarthritis signals on chromosome 15 (rs1894401)", "doi": null, "pmid": null, "metadata_source": "perplexity"
  - Why good: snippet provenance flagged, then the paper's claim is tested against the loaded LD and summary statistics rather than accepted.

- **[G9] session=d1681f3a msg=5136c9a5-336f-4783-a7d9-9800e545bb15 time=2026-07-30 19:34 user=mjdaly backend=perplexity**
  - Question: FURIN substrates in PGC/SCHEMA
  - Claim: "[Shinohara et al. 2017] | Review; per the retrieved snippet … ⚠️ snippet-level, I have not read the primary passage" … "**LRP1 has a track record of failed SCZ replication**: [Jouan et al. 2013] found no rare-variant excess, and [Stokowy et al. 2018] got p = 0.012 that did not survive correction. SCHEMA2 is the strongest LRP1 evidence to date, but it is the latest entry in an inconsistent series, not a fresh finding."
  - Source (Jouan): "We have not identified any excess of rare variants in the additional SCZ cases we have screened."
  - Why good: negative and non-replicating studies are surfaced and weighed; a proximity-artefact GWAS hit (POMC) is retracted in the same message.

- **[G10] session=d1681f3a msg=9745bd55-b3e4-4054-be35-1453a796b2ba time=2026-09-13 13:31 user=mjdaly backend=perplexity**
  - Question: role of ADAM23 in the brain
  - Claim: "The only direct evidence I found is [Goldsmith et al. 2004], which states ADAM23 maturation *'depends on cleavage by **furin or a related enzyme**'* — in rat cerebellar granule cells, 20 years ago, with the convertase not pinned down … **I would not present ADAM23 as a confirmed FURIN substrate.**" and "the human evidence is one year old and single-family"
  - Source: Hirano 2025 abstract: "we report the first ADAM23 variant associated with lethal neonatal-onset epile…"
  - Why good: species, age, and ambiguity of a single functional study named; single-family human report weighted as such.

- **[G11] session=3a212b90 msg=1b3e48f1-aa0a-4a3f-aab1-06bbbf591116 time=2026-08-04 16:02 user=mjdaly backend=perplexity**
  - Question: COL6A3 and cardiovascular disease
  - Claim: "[Luther et al. 2012, *Circ Res*] — *Col6a1*−/− mice show smaller infarcts … ; [Krstic et al. 2026] report reduced systolic function in *Col6a1*−/− rats. Direction of effect in the heart is not settled."
  - Source: not persisted
  - Why good: two contradictory animal results reported together with the contradiction stated.

- **[G12] session=c4489444 msg=da3a452a-1057-42d3-9c51-ef1ed2806689 time=2026-08-03 18:37 user=mjdaly backend=perplexity**
  - Question: ADAMTS15 and FURIN
  - Claim: "my literature search (`perplexity` backend) surfaced **no paper showing furin cleaves ADAMTS15 specifically**, and UniProt does not name furin as the protease. So 'FURIN activates ADAMTS15' is an inference from family-level biochemistry, not a documented result."
  - Source: not persisted
  - Why good: refuses to upgrade a family-level mechanism to a gene-level claim.

- **[G13] session=4a3ee8e0 msg=fa375608-6bbe-409e-90dd-b595d6ed71df time=2026-08-07 12:25 user=mjdaly backend=perplexity**
  - Question: associations génétiques de SELP
  - Claim: "Cohérent avec la littérature : Thr715Pro altère la glycosylation terminale et abaisse la P-sélectine soluble ([Subramanian et al. 2012], [Ay et al. 2007]) — la protection contre la TVP décrite dans ces études cas-témoins ne se retrouve pas ici en fine-mapping."
  - Source: not persisted
  - Why good: study design named (case-control) and the discordance with the loaded fine-mapping stated rather than smoothed over.

- **[G14] session=990622a5 msg=4a1b35bc-2d3d-4420-8abb-3c1251960af0 time=2026-08-03 17:50 user=mjdaly backend=perplexity (+web_search)**
  - Question: CTSC and CTSD
  - Claim: "Brensocatib (BRINSUPRI), a first-in-class oral DPP1/cathepsin C inhibitor, was FDA-approved 12 Aug 2025 for non-CF bronchiectasis ([Insmed](…investor.insmed.com…))"
  - Source: web_search result: "FDA Approves BRINSUPRI™ (brensocatib) as the First and Only Treatment for Non-Cystic Fibrosis Bronchiectasis … Aug 12, 2025"
  - Why good: a drug-status claim was checked with a tool and linked to a primary release instead of asserted from memory (contrast F21).

## 4. Patterns

1. **The user's "regurgitates rather than scrutinizes" complaint is not what this transcript shows.** In this user's sessions, the Perplexity backend returned structured EuropePMC records for ~85% of items, and the assistant mostly worked from abstracts. Perplexity-summary relay (category 4) occurred twice, both times labelled (F12, F15). The failure mode here is different: the assistant *describes* papers accurately in its PASS 2 tables and then, in PASS 3, **counts them instead of weighting them**. The three medium findings are all of this shape — a preprint snippet becomes one of "three independent readouts" (F5), a HEK293T reporter assay and a Drosophila habituation assay become two of "five independent systems" (F13), a structural-biology paper's passing citation becomes independent corroboration of the loaded GWAS (F19).

2. **The tier mismatch is specifically between human-genetic and functional literature.** Human association papers almost always get n and design (G1, G2, G3, G9) — the genetics rubric transfers to genetics papers. Functional/mechanistic papers (cell line, single mouse pharmacology, invertebrate, preprint) get a one-line finding and no n, replication, or system caveat in the analysis narrative, while the loaded data in the same message gets pseudo-CS warnings, PIP interpretation, exome-wide thresholds and censoring analysis every time (F5, F13, F14). There is no equivalent "is this a cell line / one mouse / unreviewed / never replicated" step; when it happens (G6, G8, G10) it is ad hoc.

3. **Provenance blending is systematic and mostly invisible.** In 5 of 15 persisted-RESULT messages the PASS 2 table mixes rows the search returned with rows or details supplied from memory (sample sizes, gene lists, PMC ids, "mouse genetics") under a single "Backend queried: perplexity" header (F2, F7, F10, F11, F21). One case is verifiable against the RESULT (F11: a PMC link the record did not contain). The PASS 2 format has no column for "retrieved vs recalled", so the reader cannot separate them; the assistant only marks the distinction when it *fails* to retrieve something (G6, G7).

4. **Reviews supply the roster, and the roster inherits the review's confidence.** Substrate lists (F15), "Lewy body core component" (F8), "PNN disruption is a recurring finding" (F20) all enter as review-derived and are never traced to a primary n. This is the closest thing in these sessions to the "demyelination based on 5 people" pattern the user described — the primary evidence behind a review sentence is never asked for.

5. **The response format helps and hurts.** The PASS 1/2/3 structure guarantees literature is separated from loaded data and that mouse/MGI evidence is labelled, which is why category-6 handling is consistently good (G2, G3, G13). But the PASS 2 table's single "Finding" column invites a one-line restatement of the abstract's conclusion; adding "design / n / replicated?" as a required column (mirroring the "cs_size / PIP / pseudo?" columns PASS 1 already has) would put the scrutiny where the rubric expects it.

6. **Not a backend artefact, not an older-vs-newer artefact.** The 8 non-persisted (older) messages show the same distribution — good design-level scrutiny of association papers (G1, G13), face-value relay of functional/MR papers (F18, F19, F20). The variable is the paper type, not the tool.

7. **Baseline rate.** 14 counter-examples across 18 literature-reporting messages versus 21 findings, 18 of them low: the assistant scrutinises literature more often than not, and the highest-risk behaviour (aggregating weak functional evidence into "converging" support in the concluding analysis) appears in roughly one message in five.
