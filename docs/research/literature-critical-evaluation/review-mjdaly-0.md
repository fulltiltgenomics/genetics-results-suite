# Literature-evaluation review — transcript `mjdaly-0.txt` (user mjdaly, 23 sessions, 2026-04-11 → 2026-06-02)

Scope: every assistant message that reports literature, from a tool result or from memory. The whole file
(6,050 lines) was read. **No `[[RESULT ...]]` block exists anywhere in this file** — every literature/web
result is "not persisted", so all sources below are judged from the assistant's text alone. The specific
complaint example ("demyelination for schizophrenia ... based on like 5 people") does not occur in this
transcript file.

## 1. Counts

| Item | Count |
|---|---|
| `search_scientific_literature` calls, total | 84 |
| — explicit `backend: perplexity` | 47 |
| — explicit `backend: europepmc` | 4 (deCODE vertigo; FCRL2 pQTL; PLCB2; MAF T-cell) |
| — no backend argument (user default `lit_backend=perplexity`; assistant's own footnotes say Perplexity) | 33 |
| `web_search` calls | 9 (6 in BIN2 session, 3 in DVT follow-up) |
| Assistant messages that ran a literature or web search | 25 |
| Literature RESULT blocks persisted | **0** |
| Assistant messages with a literature section and **no** search call | 5 clear (CNDP2, OA/GCKR, MYH7, SCHEMA burden, MAF) + 2 partial (85-variant "textbook" tier from memory; KLF6 burden "I'll skip a fresh search") |
| Messages where the text mislabels provenance ("Perplexity and Europe PMC" when only Perplexity ran) | 5 (BIN2, TNFRSF14, TREC, ADAM17 #1, SHARPIN "Perplexity (Europe PMC backend)") |
| Findings (F) | 30 — high 3, medium 13, low 14 |
| Counter-examples (G) | 17 |
| User pushback on a literature-section claim | 1 (Leu32Pro, session 20957e47) — plus 1 on a data label (DVT) that the user then withdrew |
| Borderline cases skipped | 8 (listed at end) |

Findings by category (a finding may carry several):

| Cat | Description | n |
|---|---|---|
| 1 | Face-value pass-through (no n / design / species / replication) | 9 |
| 2 | Evidence-tier mismatch vs. the hedged genetics | 5 |
| 3 | Source does not support claim / garbled or wrong citation | 8 |
| 4 | Perplexity summary treated as primary source | 4 |
| 5 | Candidate-gene / small-n association reported as support | 3 |
| 6 | Literature vs loaded data not reconciled | 3 |
| 7 | Missing / unseparable provenance | 23 |
| 8 | Reviews, consensus, lay sources accepted as the evidence | 5 |

## 2. Findings

- **[F1] session=22df04a0 msg=3119b618 time=2026-04-11 01:11:43 user=mjdaly backend=none**
- Question: What associ do you know about near CNDP2
- Claim: "The **CNDP1/CNDP2 locus on chr18q22** is a known pQTL locus for circulating CNDP1 protein levels, with the "Mannose-binding lectin" (MBL)-like trinucleotide repeat in CNDP1 linked to diabetic nephropathy risk." / "The 3′ UTR variant **18:74521112:T:C** is a well-replicated eQTL for CNDP2 expression across many tissues."
- Source: no literature tool was called; "PASS 2 — LITERATURE CONTEXT" is from memory.
- Problem: 7, 3 — no citation for either sentence; the CNDP1 nephropathy repeat is a (CTG)n leucine repeat (D18S880), "mannose-binding lectin-like" is not a description of it; "well-replicated" is an assertion the loaded eQTL table cannot supply. (Same message later does say the nephropathy link is "candidate-gene studies" — see G1.)
- Severity: medium

- **[F2] session=fc5e981f msg=28ebde1f time=2026-04-14 16:53:46 user=mjdaly backend=perplexity**
- Question: What do we know about the effects of rs200317762?
- Claim: "Tubulin mutations in related genes (e.g., *TUBA1A*, *TUBB4B*) have been linked to neurological and cochlear disorders, providing indirect biological plausibility."
- Source: not persisted (2 Perplexity calls on TUBA1C; the text says they "found **no published papers**").
- Problem: 7 — a memory-derived claim is placed after a search that reportedly returned nothing on point; no citation; reader cannot separate the two.
- Severity: low

- **[F3] session=fc5e981f msg=359f834d time=2026-04-14 17:00:16 user=mjdaly backend=europepmc**
- Question: which of these [deCODE vertigo] have evidence of association in FinnGen
- Claim: "The [Skuladottir et al. 2021] paper (Perplexity/Europe PMC) identified 6 primary loci. Key notes: **ZNF91, OTOGL, OTOP1** were specifically associated with BPPV / **OTOG, TECTA, ARMC9** were associated with broader vertigo"
- Source: not persisted (one `europepmc` call only).
- Problem: 7 — labelled "Perplexity/Europe PMC" though only Europe PMC ran; the per-locus phenotype split is presented as the paper's finding although Europe PMC returns abstracts, not results tables.
- Severity: low

- **[F4] session=f4786e23 msg=c75c2680 time=2026-04-19 23:13:14 user=mjdaly backend=perplexity**
- Question: Is there a biological connection between SHARPIN and FCRL2?
- Claim: "**Source: Perplexity (Europe PMC backend)**" ... "FCRL2 is expressed on memory B cells and is regulated by B cell activation signals. Its expression is likely subject to transcriptional control by NF-κB or NF-κB-dependent B cell differentiation programs."
- Source: not persisted (3 Perplexity calls).
- Problem: 7, 4 — provenance label conflates the two backends; a speculative regulatory step is inserted into the numbered "connecting the genetic data to the literature" chain without a source. Mitigated by the Caveats section (G2).
- Severity: low

- **[F5] session=3ffe77b0 msg=3c63b15c time=2026-04-22 17:45:40 user=mjdaly backend=perplexity**
- Question: do you know of any associations to the E190K variant in the gene BIN2
- Claim: "*(Search performed via Perplexity and Europe PMC)*" ... "**[Frontiers in Genetics 2025]** — BIN2 identified as a psoriasis-related gene highly expressed in CD4+ T cells via single-cell RNA-seq and Mendelian randomization."
- Source: not persisted (2 Perplexity + 6 web_search; no Europe PMC call).
- Problem: 7, 1 — wrong provenance label; a bioinformatic scRNA+MR paper relayed with no design critique or sample size.
- Severity: low

- **[F6] session=7131a0b6 msg=177f82a1 time=2026-04-22 23:58:49 user=mjdaly backend=perplexity** (user rating 5: "great summary - makes sense")
- Question: describe any associations published or directly implicating the gene DNM1L
- Claim: "The literature (Pass 2) confirms that most pathogenic variants are **de novo heterozygous missense variants**, with hotspots at **p.Arg403** and **p.Gly362**." / Summary table: "Parkinson's disease | No association | Not supported" (from "Burden analysis in Chinese PD cohorts")
- Source: not persisted (1 Perplexity call); cited rows include "[dnm1l.org]" ("DNM1L Foundation: 58 global cases") and "[Physicians Weekly]" (news write-up of a case report).
- Problem: 4, 7, 8, 2 — a Perplexity digest is "the literature confirms"; a patient-foundation site and a news site are listed as references; one Chinese burden study becomes a flat "No association" verdict. Contrast the same message's Genebass caveat: "AC=1–3 ... should be interpreted with extreme caution".
- Severity: medium

- **[F7] session=7131a0b6 msg=a734c535 time=2026-04-23 00:03:36 user=mjdaly backend=perplexity**
- Question: what about the related gene OPA1, does this have any genetic findings?
- Claim: "prevalence ~1:12,000; 70–90% of DOA due to OPA1 variants | [MedlinePlus], [PMC10645107]" / "OPA1 encodes a mitochondrial inner membrane GTPase ... | [ADOA Foundation], [EyeWiki]" → Pass 3: "Prevalence ~1:12,000, making it the **most common inherited optic neuropathy**."
- Source: not persisted.
- Problem: 8, 7 — numbers sourced to MedlinePlus, EyeWiki and a foundation website, then restated as fact. The facts are mainstream, but nothing distinguishes them from a primary-literature claim.
- Severity: low

- **[F8] session=7131a0b6 msg=df3c20ba time=2026-04-23 00:15:32 user=mjdaly backend=perplexity**
- Question: generate the same report for MFN1 and MFN2
- Claim: "2025 study: Mutant MFN2 loses non-fusion role in proteostasis ... MFN1 lacks this function, explaining CMT2A specificity | [Medical Xpress 2025]" → Pass 3: "MFN2 uniquely ... has a proteostasis role (interacting with proteasomes/chaperones) that MFN1 cannot compensate for. This explains why MFN2 mutations cause disease despite the presence of MFN1."
- Source: not persisted; the reference is a press release; "MFN1 overexpression ameliorates CMT2A symptoms in mouse models" is sourced to "[MDA Quest]".
- Problem: 1, 4, 7 — a single new study known only via a press release becomes *the* explanation of CMT2A gene specificity; no primary citation, model system unstated.
- Severity: medium

- **[F9] session=20957e47 msg=f96a0b31 time=2026-04-25 15:17:40 user=mjdaly backend=perplexity** (user rating 5 on the conversation)
- Question: what associations or published functional studies can you tell me about this variant 1:2562891:G:A
- Claim: "*(Literature searched via Perplexity and Europe PMC)*" ... "**No published functional studies** specifically characterizing the molecular effect of this missense variant (p.Leu32Pro or equivalent) were identified." Also: "A 2024 GWAS meta-analysis of 81,718 autoimmune hypothyroidism cases identified a signal at 1:2562891 G>A (p = 6.82×10⁻²¹) ... ([PMC12987720])"
- Source: not persisted; Europe PMC was never called; the second Perplexity query was literally `"TNFRSF14 rs2234167 p.Leu32Pro autoimmune thyroid coeliac GWAS"` — the invented annotation was fed into the search.
- Problem: 7, 3 — p.Leu32Pro was fabricated (next turn: "**The honest answer: I fabricated it, and I apologise.**"); the literature pass was seeded with it. The correction then asserts "**p.Val241Ile** ... is consistent with what is reported in the literature for rs2234167" — again with no citation. The PMC id for a "2024" meta-analysis (PMC12987720) is implausibly recent and unverifiable.
- **User pushback** (msg c22a5af6): "you refer to Leu32Pro but this variant I thought was Val241Ile - where did you find the Leu32Pro annotation?"
- Severity: high

- **[F10] session=20957e47 msg=f96a0b31 time=2026-04-25 15:17:40 user=mjdaly backend=perplexity**
- Question: (same)
- Claim: "The *TNFRSF14* locus has been reported at genome-wide significance in coeliac disease GWAS, with roles in thymic T-cell selection alongside *ETS1* and *RUNX3*. The locus is also shared with rheumatoid arthritis."
- Source: not persisted; no citation given for this item (items 2–4 have links, item 1 does not).
- Problem: 7 — the one item that carries the interpretation of the coeliac signal is the only uncited one.
- Severity: low

- **[F11] session=418af678 msg=273df12d time=2026-04-28 16:05:20 user=mjdaly backend=perplexity**
- Question: what can you tell me about T-cell receptor excision circles and how they have been used to study the genetics of immune function?
- Claim: "(all searches conducted via **Perplexity** and **Europe PMC**)" vs footnote "All literature searches used the **Perplexity** backend." / "This is one of the first demonstrations that a **common genetic variant in thymic function has measurable consequences for infectious disease outcomes**" (Science Advances 2023, rs2204985/COVID-19) / "low output links to accelerated aging and mortality risk ([JCI Insight, PMID 40591412])"
- Source: not persisted (6 Perplexity calls).
- Problem: 1, 4, 7 — no sample size for the COVID-19 study or the aging/mortality claim; Perplexity's editorial framing ("one of the first demonstrations") relayed verbatim; contradictory backend labels in one message. Contrast G6 in the same message.
- Severity: medium

- **[F12] session=418af678 msg=5ea81c67 time=2026-05-11 18:01:35 user=mjdaly backend=none**
- Question: Is there any evidence that this variant [rs2204985] is associated to any human phenotypes?
- Claim: "Given the 2023 Science Advances paper linking this variant to COVID-19 severity, one might expect to see a signal in infectious disease phenotypes. The absence here likely reflects the relatively modest effect size on disease risk and the need for very large sample sizes"
- Source: loaded data: "**No GWAS associations found** ... The PheWAS plot could not be generated (no associations with -log10(p) ≥ 2.0)."
- Problem: 6 — the loaded biobank data are null at −log10p<2 across all phenotypes; the assistant resolves the conflict by assuming the published (small-cohort) finding is true with a small effect, never entertaining that the paper may not replicate.
- Severity: medium

- **[F13] session=d581d6ff msg=7f3cdd58 time=2026-04-29 14:04:45 user=mjdaly backend=perplexity**
- Question: find which of these [18 IBD exome variants] has convincing evidence of association to other phenotypes ... or in the literature
- Claim: "Rare NCF2 missense variants found in 4% of VEO-IBD patients vs 0.2% controls (OR 23.8, p=1.3×10⁻⁵) ... | Strong — multiple independent studies, functional validation" / "**IFITM3** ... Polymorphisms associated with UC susceptibility. ... | Moderate — functional mouse data + human polymorphism data ([PMID:28435159])"
- Source: not persisted (6 Perplexity calls).
- Problem: 1, 5 — a small VEO-IBD candidate-gene comparison is graded "Strong" with no n; a candidate-SNP "polymorphism" UC association is counted as human support without noting such studies mostly do not replicate. The "Strength" column grades biology, not study design.
- Severity: medium

- **[F14] session=c1b3f088 msg=8c9b1d0b time=2026-04-30 22:18:36 user=mjdaly backend=perplexity**
- Question: Does this [ADAM17] variant colocalize with any molecular QTLs ... what process might be implicated?
- Claim: "A study identified an **ADAM17 haplotype** associated with clinical response to infliximab in Crohn's disease" (no citation) / "[D'Alessandro et al. (2024)] further showed that SNPs in *TNFRSF1A* predict anti-TNF treatment discontinuation" → Pass 3: "TNFRSF1A polymorphisms have been directly linked to anti-TNF response in IBD (Pass 2), reinforcing the relevance of this pathway."
- Source: not persisted (3 Perplexity calls).
- Problem: 5, 7 — pharmacogenetic candidate-SNP studies (typically n<300, rarely replicated) cited without n and upgraded to "directly linked"; the haplotype study has no citation at all. Same message contains G7.
- Severity: medium

- **[F15] session=8271142c msg=e13d6cc4 time=2026-05-05 20:25:27 user=mjdaly backend=perplexity** (user rating 5: "excellent annotation and valuable concluding table with four tiers of evidence!")
- Question: annotate these [17 coding variants] and tell me which have conclusive evidence of association to any disease or phenotype
- Claim: "**ZAP70 T155M**: ZAP70 rs13420683 associated with Crohn's disease in a Tunisian cohort (OR=2.25 for allele C, p=0.003)." / "**MUC3A frameshift**: MUC3A SNPs associated with CD susceptibility (P=0.013) in a 2001 study." → Summary: "MUC3A | frameshift | No CS (indel); MUC3A-IBD literature; large beta | **Direct (barrier)**" / "H3Q (rs12252) is a well-studied polymorphism affecting IFITM3 function."
- Source: not persisted (8 Perplexity calls).
- Problem: 5, 3, 2 — a p=0.003 single-cohort SNP (a different SNP from T155M) and a 2001 P=0.013 candidate-gene result are recorded as IBD literature support, the latter producing "Direct" IBD relevance in the final table; rs12252 is the well-known IFITM3 variant at a different position — nothing in the tool output established that 11:320805:G:T is rs12252. Meanwhile PASS 1 hedges "`segdup` ... genotyping may be unreliable" and "pseudo credible sets".
- Severity: medium

- **[F16] session=8271142c msg=e13d6cc4 time=2026-05-05 20:25:27 user=mjdaly backend=perplexity**
- Question: (same)
- Claim: "**NCF2 H389Q**: Well-documented in VEO-IBD and CGD literature ... Rare NCF2 variants (including H389Q+N419I compound het) cause reduced neutrophil chemotaxis and are found in VEO-IBD patients." → Tier 2: "H389Q is documented in VEO-IBD patients with NADPH oxidase dysfunction" → Summary: "NCF2 | H389Q | ... VEO-IBD literature | **Direct (VEO-IBD)**"
- Source: loaded data in the same message: "NCF2 | chr1:183563445:G:T | H389Q | missense | **0.051** [gnomAD FAF NFE]"
- Problem: 6, 2 — a 5%-frequency allele is framed as a rare VEO-IBD variant; the loaded frequency and the literature framing are incompatible and the assistant does not reconcile them, then labels the IBD relevance "Direct".
- Severity: high

- **[F17] session=cb867f84 msg=20e6d467 time=2026-05-05 20:49:00 user=mjdaly backend=none**
- Question: what associations do you know about with the gene MYH7
- Claim: "It is the **most commonly mutated gene in familial HCM**, accounting for ~35–40% of cases"
- Source: no literature tool called; "PASS 2 — LITERATURE CONTEXT: The associations above are well-established in the literature".
- Problem: 7, 3 — no search, no citation; in most HCM cohorts MYBPC3 is the most frequently mutated gene (~40%) and MYH7 second (~20–30%). Stated as textbook fact.
- Severity: medium

- **[F18] session=3aa44536 msg=a9ab7d99 time=2026-05-08 17:56:16 user=mjdaly backend=perplexity**
- Question: can you evaluate the connection of SLC6A3 to human disease?
- Claim: "**Rare variant functional study** — [Hansen et al. 2014 (JCI)](https://www.jci.org/articles/view/39060): Identified homozygous missense mutations (p.L368Q, p.P395L) in consanguineous families with infantile parkinsonism-dystonia."
- Source: not persisted (2 Perplexity calls).
- Problem: 7, 3 — JCI article 39060 with L368Q/P395L is Kurian et al. 2009; Hansen et al. 2014 is a different JCI paper (adult parkinsonism/ADHD DAT missense variants). Author, year and finding are mismatched; unverifiable because nothing was persisted. Otherwise this message is the best counter-example (G8).
- Severity: low

- **[F19] session=3aa44536 msg=6cd3a9b1 time=2026-05-08 18:39:13 user=mjdaly backend=none**
- Question: show the schizophrenia rare-variant burden results with p<10^-6 and tell me if there are any schizophrenia GWAS hits nearby to those genes
- Claim: "The SCHEMA consortium published their landmark paper in *Nature* 2022 (Singh et al.), identifying 10 genes ..." / "**SETD1A** ... Also a known GWAS locus in PGC." / "the PGC GWAS (which does have hits near several of these genes, e.g., SETD1A at 16p11.2)" / "**CUL1, PPP3CA ... ATP9A** — nominated in SCHEMA or subsequent analyses."
- Source: no literature tool called.
- Problem: 7 — the user's actual question (GWAS hits near these genes) could not be answered from the loaded data and is answered from memory with no PMID or locus list; "nominated in SCHEMA or subsequent analyses" is unfalsifiable.
- Severity: medium

- **[F20] session=846ddc62 msg=bac30cba time=2026-05-12 00:45:44 user=mjdaly backend=none**
- Question: what genetic associations implicate the gene MAF
- Claim: "**Thyroid disease** — MAF is a well-established GWAS locus for thyroid disorders. The transcription factor MAF is expressed in thyroid follicular cells and has been implicated in thyroid development and autoimmunity." → Pass 3: "The causal mechanism likely involves MAF's role as a transcription factor in thyroid follicular cells and immune cells."
- Source: no literature tool called.
- Problem: 7 — the thyroid-follicular-cell expression/development role has no citation and is not an established MAF function (MAF's characterized roles are lens, kidney, chondrocyte and T-helper biology); it is then made the mechanism in the conclusion.
- Severity: medium

- **[F21] session=846ddc62 msg=c2c498c2 time=2026-05-12 00:56:52 user=mjdaly backend=perplexity+europepmc**
- Question: do the T-cell eQTLs affecting MAF colocalize with any phenotypes or do those lead variants appear in literature-based associations?
- Claim: "The literature strongly supports a model where **MAF expression level in CD8+ TEM cells is a key determinant of T cell exhaustion**. Higher MAF → more PD-1/LAG-3/IL-10 → more exhaustion → poorer tumor control and potentially altered autoimmune susceptibility."
- Source: not persisted; cited: "[MAF drives CD8+ T-cell exhaustion in melanoma] ... *maf*⁻/⁻ CD8+ T cells show better tumor control" (mouse), "[c-Maf: a checkpoint that programs autoimmunity] (~2024 review)", "[c-MAF: A Swiss Army Knife ...] (*Frontiers in Immunology*, 2020)" (review).
- Problem: 1, 8, 2 — one mouse tumour model plus two reviews become "the literature strongly supports"; species/model never stated; the autoimmunity extension is the review's framing. Same message hedges the genetics tightly ("**No — not formally.** All three lead variants returned empty colocalization results").
- Severity: medium

- **[F22] session=9a156c35 msg=7253e7fb time=2026-05-15 00:12:51 user=mjdaly backend=perplexity (default)**
- Question: is the BCL11A variant the same one that leads to fetal hemoglobin persistence and eventually the cure for sickle cell disease?
- Claim: "**[Sankaran et al., Blood 2011]** — Identified a **14-kb region in intron 2** of BCL11A as an erythroid-specific enhancer ... The causal variants (rs1427407, rs7606173, rs6706648) lie within this enhancer." / "**[Canver et al., Science 2013]** — Fine-mapped the GWAS signal to specific motif-disrupting variants ... required for BCL11A expression in **erythroid cells but not B-lymphoid cells**" / "**Steinberg et al., Human Molecular Genetics 2016** — Reviews ..."
- Source: not persisted (1 call, no backend argument).
- Problem: 7, 3 — the intron-2 erythroid enhancer and its erythroid-vs-B-cell requirement are Bauer et al., Science 2013; Canver et al. is Nature 2015; the "Blood 2011" link points at Blood volume 120 (2012). Author/journal/year combinations are garbled and unverifiable. The scientific answer itself (different variant, r²<0.1) is data-driven and correct.
- Severity: low

- **[F23] session=6ec930f3 msg=ccbf6f1d time=2026-05-16 01:51:05 user=mjdaly backend=perplexity**
- Question: any disease or phenotype associations that might implicate the gene SPN (also known as CD43)?
- Claim: Summary table: "Wiskott-Aldrich syndrome | Literature (functional) | ⭐⭐ Moderate | High (but SPN is not causative)" / "**Coding polymorphisms**: A [1998 paper (PMID 9876225)] reported two common coding polymorphisms in CD43, relevant to population-level variation."
- Source: not persisted (1 Perplexity call).
- Problem: 8, 7 — the summary table scores WAS as "Moderate" evidence for an SPN–disease association immediately after the text established SPN is not the causative gene (G9); the 1998 polymorphism paper is cited as evidence of nothing in particular.
- Severity: low

- **[F24] session=40d9d693 msg=d3f79eab time=2026-05-21 15:25:52 user=mjdaly backend=perplexity**
- Question: (identical to session c1b3f088, three weeks earlier) Does this [ADAM17] variant colocalize with any molecular QTLs ... what process might be implicated?
- Claim: "**ERBB4** and other EGFR-family receptors and ligands are well-documented ADAM17 substrates (Pass 2: JCI 2017)." / "Established biology: ADAM17 (TACE) is the major sheddase that releases the ectodomains of ... EGFR-family ligands (relevant to ERBB4 signalling), and many other type-I membrane proteins including L-selectin, IL-6Rα, TGFα, heparin-binding EGF, and **NTRK family receptors**."
- Source: not persisted (1 Perplexity call); the JCI 2017 reference is described as "iRhom2 promotes lupus nephritis via ADAM17/TNF-α/EGFR" — about TNF and EGFR *ligands*, not the ERBB4 receptor.
- Problem: 3, 6, 7 — on 2026-04-30 the same question got "ERBB4 is **not a well-established direct substrate** of ADAM17 shedding — ADAM10 is more commonly implicated"; here ERBB4 is "well-documented", and "NTRK family receptors" as ADAM17 substrates is uncited. Two opposite mechanistic readings of the same pQTL, three weeks apart, neither flagged as uncertain. Also uncited: "loss of ADAM17 in mice causes severe intestinal inflammation ... rare loss-of-function ADAM17 mutations in humans cause neonatal inflammatory skin and bowel disease".
- Severity: high

- **[F25] session=de6315ee msg=e04d2767 time=2026-05-24 20:09:31 user=mjdaly backend=perplexity (default)**
- Question: scan through existing studies and the literature to see which of these [85 IBD variants] have no existing reported or obvious connections to IBD
- Claim: "**BTBD8** | V60I | **Direct prior paper**: Btbd8 KO mice protected from DSS colitis; reduced expression in inflamed UC ([2024 *Cell Death & Disease*, PMC10978791]). Not novel." / "**GCKR** | L446P (P446L) | Highly pleiotropic metabolic variant ([Beer et al. 2009 — pleiotropy review](https://www.frontiersin.org/journals/endocrinology/articles/10.3389/fendo.2023.1247611/full))" / "**ADCY7** | D439E | Established UC missense — published mechanism: ADCY7 D439E reduces cAMP, restrains T cell inflammation" (no citation) / Tier A: 29 genes labelled "Textbook IBD genes" with no citations ("**CFTR** ... CD enrichment reported in multiple papers").
- Source: not persisted (28 calls, no backend argument; assistant: "Literature searches used the Perplexity backend; Europe PMC results were sparse").
- Problem: 1, 7, 8 — one mouse DSS paper settles "Not novel"; a "Beer et al. 2009" label is attached to a 2023 Frontiers URL; the largest tier is memory with zero citations; the novelty verdict the user asked for rests on Perplexity's coverage of niche multi-gene queries. Mitigated by the message's own caveats (G10).
- Severity: medium

- **[F26] session=dc98318a msg=f6c65720 time=2026-05-27 15:34:38 user=mjdaly backend=perplexity (default)**
- Question: Do any of the credible sets in the three largest BMD studies above overlap with PGC schizophrenia credible sets
- Claim: "[Liu et al., *Genomic Psychiatry* 2026](https://academic.oup.com/gpb/article/23/5/qzaf097/8315144) — ... Reports **195 shared loci** between SCZ and osteoporosis-related traits; **heel BMD shows the strongest overlap** with SCZ (140 shared loci at variant level, mapping to 1,376 protein-coding genes)." / "[Liu et al., *Clin Transl Med* 2020](https://pmc.ncbi.nlm.nih.gov/articles/PMC5796546/)"
- Source: not persisted; each item tagged "(Backend: Perplexity)".
- Problem: 7 — journal names do not match the URLs (academic.oup.com/gpb is *Genomics, Proteomics & Bioinformatics*; PMC5796546 is a ~2018 id, not 2020); specific counts relayed from the summary with no sign the paper was opened. The KLF6 conclusion is then said to be "consistent with the recent Liu et al. 2026 ... finding".
- Severity: low

- **[F27] session=dc98318a msg=146a5d40 time=2026-05-27 15:44:35 user=mjdaly backend=perplexity**
- Question: Are there any molecular QTL studies that implicate function of this [KLF6] variant?
- Claim: "KLF6 is reported as **lower in osteosarcoma** than normal bone ([Lin et al. PMC3460099]), suggesting KLF6 plays a role in bone tissue."
- Source: not persisted (2 Perplexity calls).
- Problem: 1 — a tumour-vs-normal expression comparison is offered as evidence for a role in normal bone/BMD biology; a non sequitur, though the same message concedes "direct evidence for KLF6 in osteoblast differentiation is weak" (G12).
- Severity: low

- **[F28] session=dc98318a msg=62fa715a time=2026-05-27 15:46:46 user=mjdaly backend=perplexity**
- Question: could you check pQTL, methylation QTLS, etc as well?
- Claim: "[Wu et al., *Cancer Genetics* 2014 (PMID 24378751)] — The KLF6 3′ UTR contains regulatory elements that **destabilize KLF6 mRNA**. Specific 3′ UTR sequence (positions 1,835–2,615) impairs reporter expression." → "KLF6's 3′ UTR is known to destabilize KLF6 mRNA ... providing a plausible functional mechanism."
- Source: not persisted (1 Perplexity call).
- Problem: 1 — a reporter assay (cell line unstated, cancer-genetics context) generalised to a mechanism for rs17731 in bone/brain; no note that the 3′ UTR position of rs17731 relative to the 1,835–2,615 element was checked. Partly offset by G13.
- Severity: low

- **[F29] session=dc98318a msg=6b9a5fa8 time=2026-05-27 15:50:05 user=mjdaly backend=none**
- Question: Could you check rare-variant burden evidence for KLF6 in SCHEMA and GeneBass?
- Claim: "KLF6 is a transcription factor and is moderately constrained against LoF in gnomAD (pLI ~0.9 in earlier releases; haploinsufficient TFs are often constrained)." / "I'll skip a fresh search here — the previous searches in this conversation already established that KLF6's reported disease links are dominated by common-variant GWAS and somatic-cancer biology, with no Mendelian or rare-variant disease association reported in major databases."
- Source: none; the assistant says "(worth checking, though I haven't pulled gnomAD constraint in this thread)".
- Problem: 7 — a specific number (pLI ~0.9) from memory is used in the argument; the "no association in major databases" claim rests on two earlier narrow Perplexity queries about brain and osteoblasts, not on a database lookup (`get_gene_disease_associations` was available and not called).
- Severity: low

- **[F30] session=682ca7fd msg=dc1d8c5d time=2026-04-16 20:31:47 user=mjdaly backend=none**
- Question: tell me if any of these [OA proteins] have pQTLs and if any of those pQTLs have any association to osteoarthritis
- Claim: "The chr2:27.5 Mb locus is a well-known metabolic/urate locus near *GCKR* (glucokinase regulatory protein) and *ABCG5/ABCG8* (cholesterol/bile acid transporters)." / "The chr12:111 Mb locus is the well-established **SH2B3/ATXN2** region"
- Source: no literature tool called.
- Problem: 7, 3 — "PASS 2 — LITERATURE CONTEXT" is memory with no citation; ABCG5/ABCG8 lie at chr2:43.8 Mb, ~16 Mb from the 27.5 Mb pQTL lead. The trans-pQTL interpretation that follows is reasonable but rests on uncited background.
- Severity: low

## 3. Counter-examples

- **[G1] session=22df04a0 msg=3119b618 time=2026-04-11 01:11:43 user=mjdaly backend=none**
- Question: What associ do you know about near CNDP2
- Claim: "though CNDP1/CNDP2 have been implicated in diabetic nephropathy in candidate-gene studies — this would require a separate literature search to confirm GWAS evidence"
- Source: memory, flagged as such.
- Good behaviour: names the candidate-gene era and declines to treat it as GWAS-grade.

- **[G2] session=f4786e23 msg=c75c2680 time=2026-04-19 23:13:14 user=mjdaly backend=perplexity**
- Question: Is there a biological connection between SHARPIN and FCRL2?
- Claim: "The mechanistic pathway (SHARPIN → NF-κB → FCRL2 transcription) is **inferred**, not directly demonstrated. No published paper directly links SHARPIN to FCRL2 expression." / "trans ... can sometimes reflect confounding (e.g., shared immune cell composition effects)"
- Source: not persisted.
- Good behaviour: separates what the papers show from the assistant's own synthesis.

- **[G3] session=f4786e23 msg=1826cf0e time=2026-04-19 23:16:06 user=mjdaly backend=perplexity+europepmc**
- Question: where does plasma FCRL2 originate from?
- Claim: "No published literature describes a secreted or soluble isoform" / "**Aptamer/antibody cross-reactivity** (assay artifact) ... cannot be excluded" / "The **enormous cis-pQTL effect** ... raises the possibility of **aptamer binding artifacts**"
- Source: not persisted.
- Good behaviour: reports an absence-of-evidence result as such and turns the scrutiny on the loaded data too.

- **[G4] session=7131a0b6 msg=df3c20ba time=2026-04-23 00:15:32 user=mjdaly backend=perplexity**
- Question: generate the same report for MFN1 and MFN2
- Claim: "⚠️ The absence of ClinGen/GENCC entries for MFN2 likely reflects a curation gap rather than lack of evidence — the CMT2A–MFN2 association is one of the best-established in peripheral neuropathy genetics."
- Source: loaded ClinGen/GENCC returned nothing for MFN2.
- Good behaviour: a category-6 conflict (loaded data vs literature) explicitly noticed and reconciled.

- **[G5] session=5eda2679 msg=5b0a3b13 time=2026-04-27 17:16:06 user=mjdaly backend=perplexity**
- Question: Is there genetic evidence the plasma leptin levels are associated to BMI?
- Claim: "[Kilpelä et al. 2019, *Int J Mol Sci*] | Observational: BMI-leptin correlation ... | Moderate: candidate gene + GWAS, n=1,011" / "[Leyden et al. 2023, *eLife*] | **Mendelian randomization** ... (β=0.51 in adulthood, 95% CI 0.32–0.69, p=1×10⁻⁷) | Strong: MR design, large sample, tissue-partitioned"
- Source: not persisted.
- Good behaviour: a "Strength" column grading each paper by design, n, and effect size with CI — the format the rubric asks for, produced spontaneously.

- **[G6] session=418af678 msg=273df12d time=2026-04-28 16:05:20 user=mjdaly backend=perplexity**
- Question: TRECs and immune genetics
- Claim: "**Cohort**: 1,000 healthy adults from the **Milieu Intérieur** cohort" / "(The Milieu Intérieur cohort of 1,000 individuals was relatively small for a GWAS)"
- Source: not persisted.
- Good behaviour: sample size stated and its consequence drawn for the landmark GWAS (though not for the other studies in the same message — F11).

- **[G7] session=c1b3f088 msg=8c9b1d0b time=2026-04-30 22:18:36 user=mjdaly backend=perplexity**
- Question: ADAM17 variant QTL colocalization
- Claim: "However, ERBB4 is **not a well-established direct substrate** of ADAM17 shedding — ADAM10 is more commonly implicated in ErbB4 ectodomain cleavage." / "A narrative review ([PMC10237104]) describes ..."
- Source: not persisted.
- Good behaviour: pushes back on the obvious pQTL-to-substrate reading and labels a review as a review. (Reversed without comment in F24.)

- **[G8] session=3aa44536 msg=a9ab7d99 time=2026-05-08 17:56:16 user=mjdaly backend=perplexity**
- Question: evaluate the connection of SLC6A3 to human disease
- Claim: "[Müller et al. 2008]: The 9-6 VNTR haplotype ... associated with adult ADHD in 216 cases vs. 528 controls. Candidate gene study, not GWAS." / "[Lahti et al. 2017] ... Small sample (n=546)." / "ADHD candidate gene | ⭐⭐ **Weak** | VNTR associations; not replicated in GWAS" / "Large-scale ADHD GWAS did not identify SLC6A3"
- Source: not persisted.
- Good behaviour: the clearest instance of the requested baseline — n, design, replication status against a large GWAS, and a down-weighted verdict.

- **[G9] session=6ec930f3 msg=ccbf6f1d time=2026-05-16 01:51:05 user=mjdaly backend=perplexity**
- Question: SPN/CD43 associations
- Claim: "However, this is a functional abnormality secondary to WASp protein deficiency — **SPN itself is not the causative gene** for Wiskott-Aldrich syndrome ... This is confirmed by the absence of SPN in ClinGen/GENCC Mendelian disease databases."
- Source: not persisted.
- Good behaviour: cross-checks a 1989 literature link against the loaded curation database and rejects the naive reading (undercut by the summary table — F23).

- **[G10] session=de6315ee msg=e04d2767 time=2026-05-24 20:09:31 user=mjdaly backend=perplexity (default)**
- Question: 85-variant IBD novelty triage
- Claim: "'Novel in literature' ≠ 'causal'." / "Literature searches used the Perplexity backend; Europe PMC results were sparse for many of these niche queries. A more rigorous follow-up would use Europe PMC + manual review for the Tier C candidates."
- Source: not persisted.
- Good behaviour: states the backend's limits for the exact kind of query (absence claims) where they matter most.

- **[G11] session=dc98318a msg=f6c65720 time=2026-05-27 15:34:38 user=mjdaly backend=perplexity (default)**
- Question: BMD × PGC SCZ credible-set overlap
- Claim: "I did **not** find a paper specifically implicating KLF6 or MMD in SCZ–BMD pleiotropy." / "though I could not locate a paper that specifically calls out KLF6"
- Source: not persisted.
- Good behaviour: declines to manufacture literature support for the top hit.

- **[G12] session=dc98318a msg=146a5d40 time=2026-05-27 15:44:35 user=mjdaly backend=perplexity**
- Question: KLF6 variant function
- Claim: "the strongest direct evidence is for KLF5/KLF7, not KLF6. No primary study directly links KLF6 expression changes to schizophrenia in the literature I retrieved." / "KLF family members regulate osteoblast differentiation, but KLF2/3/4/10/15 are emphasized rather than KLF6." / "direct evidence for KLF6 in osteoblast differentiation is weak"
- Source: not persisted.
- Good behaviour: distinguishes the queried gene from its paralogs in reviews rather than borrowing their evidence.

- **[G13] session=dc98318a msg=62fa715a time=2026-05-27 15:46:46 user=mjdaly backend=perplexity**
- Question: pQTL / mQTL for KLF6
- Claim: "I did **not** find a paper that specifically characterizes rs17731 functionally. (Backend: Perplexity)" / "**Not measured.** ... the absence of pQTL evidence reflects panel coverage, not a negative result."
- Source: not persisted.
- Good behaviour: absence of evidence labelled and its cause (panel coverage) given.

- **[G14] session=20957e47 msg=8f0449b0 time=2026-04-25 15:31:38 user=mjdaly backend=none**
- Question: where did you find the Leu32Pro annotation?
- Claim: "**The honest answer: I fabricated it, and I apologise.** ... this was a hallucination on my part. It was not grounded in any tool result." / "I should have written: *'the specific amino acid change was not returned by the annotation tools ...'*"
- Source: two gnomAD re-queries returned no HGVSp.
- Good behaviour: after pushback, a full retraction and the correct alternative sentence — but only after pushback, and the replacement claim is itself uncited (F9).

- **[G15] session=d581d6ff msg=7f3cdd58 time=2026-04-29 14:04:45 user=mjdaly backend=perplexity**
- Question: 18 IBD variants
- Claim: "The tool's nearest-gene lookup revealed that **3 variants do not overlap the gene listed in your table**" / "⚠️ **Note:** The database nearest-gene tool places this variant in *SMG7*, not NCF2 — please verify"
- Source: loaded `analyze_variant_list` output.
- Good behaviour: the user's own annotation is checked against the data rather than accepted (contrast F16, where the same NCF2 variant's frequency was not checked against the VEO-IBD framing).

- **[G16] session=7131a0b6 msg=177f82a1 time=2026-04-22 23:58:49 user=mjdaly backend=perplexity**
- Question: DNM1L associations
- Claim: "De novo p.Arg403Cys mutation in two unrelated patients" / "Novel variant expanding clinical phenotypic spectrum (case report)"
- Source: not persisted.
- Good behaviour: n=2 and "case report" stated in the table (even though the same table cites dnm1l.org and Physicians Weekly — F6).

- **[G17] session=8271142c msg=e13d6cc4 time=2026-05-05 20:25:27 user=mjdaly backend=perplexity**
- Question: 17 IBD variants
- Claim: "in a Tunisian cohort (OR=2.25 for allele C, p=0.003)" / "(P=0.013) in a 2001 study"
- Source: not persisted.
- Good behaviour (partial): the weak p-values and the year are transcribed — the reader *can* discount them; the assistant's own tiering did not (F15).

## 4. User pushback

- 20957e47 msg c22a5af6 (2026-04-25 15:31:38): "you refer to Leu32Pro but this variant I thought was Val241Ile - where did you find the Leu32Pro annotation?" → assistant retracted (G14).
- 20957e47 msg 199e4f37 (15:49:24): "is there really a DVT association or is this from some stupid combo DVT+asthma+mumble phenotype in UKBB?" — about a loaded-data label, not literature; the assistant could not resolve the phenotype definition; user then said (a83345c8) "I think it was fine to report as you did".
- No other USER message disputes a literature claim. Two sessions containing findings F6 and F15/F16 were rated 5 by the user.

## 5. Patterns

1. **Provenance is unrecoverable, and the assistant's own labels make it worse.** Nothing was persisted (0 RESULT blocks across 84 calls), and the text misstates which backend ran in 5 messages ("Perplexity and Europe PMC" when only Perplexity ran; "Perplexity (Europe PMC backend)"). The four genuine Europe PMC calls were always in messages that also called Perplexity, and no message separates what came from which. Garbled citations — Hansen/Kurian (F18), "Canver et al., Science 2013" and "Sankaran Blood 2011" (F22), "Beer et al. 2009" on a 2023 URL (F25), "Genomic Psychiatry" on a GPB URL (F26), "Richard et al." (skipped) — are the fingerprint of a Perplexity digest being blended with model memory and then re-written with author/year confidence the model does not have.

2. **The asymmetry the user complains about is structural, not occasional.** PASS 1 carries a fixed vocabulary of caveats in nearly every message — "pseudo credible sets", "AC=1–3 ... extreme caution", "segdup", "PIP is low", "winner's curse", "formal colocalization would be needed". PASS 2 is a table of one-line findings with a link, and has no column for n, design, species, or replication. When the model *does* grade papers (G5 leptin, G8 SLC6A3, the "Strength" column in F13), it does so ad hoc and inconsistently — in F13 the "Strength" column grades biology, not the study. The capability exists; the rubric does not. The fix is a literature-evidence rubric in the prompt (n, design, species/system, replication, primary-vs-review, lay-vs-peer-reviewed) enforced by the response format, mirroring the one already applied to PIPs and p-values.

3. **Perplexity's web sources are relayed as if they were papers.** MedlinePlus, EyeWiki, ADOA Foundation, dnm1l.org ("58 global cases"), Physicians Weekly, Medical Xpress, MDA Quest, GeneCards, Protein Atlas all appear in "PASS 2 — LITERATURE" tables with the same typographic weight as PMIDs (F6, F7, F8). A press release became the explanation of CMT2A gene specificity (F8). This is specifically a Perplexity-backend effect; the europepmc calls did not produce this.

4. **Candidate-gene / small-n human associations are never discounted unless the model happens to remember the field's consensus.** MUC3A 2001 P=0.013 → "Direct (barrier)"; a Tunisian ZAP70 SNP p=0.003; IFITM3 "polymorphisms associated with UC"; NCF2 "OR 23.8" with no n; TNFRSF1A pharmacogenetic SNPs → "directly linked" (F13–F16). The one place the model wrote "Candidate gene study, not GWAS ... not replicated in GWAS" (G8, SLC6A3/ADHD) is a field where the non-replication is itself famous. The knowledge that such studies mostly fail is applied from memory, not from a rule.

5. **Memory substitutes for search in exactly the sections labelled "LITERATURE".** Five messages produce a "PASS 2 — LITERATURE CONTEXT" with no search at all (F1, F17, F19, F20, F30); two contain checkable factual errors (MYH7 "most commonly mutated gene in familial HCM"; ABCG5/ABCG8 "near" chr2:27.5 Mb) and one an unsupported mechanism (MAF "expressed in thyroid follicular cells") that becomes the conclusion. The fabricated p.Leu32Pro (F9) was fed into a search query, so the search itself was contaminated. The three-pass template appears to *require* a literature section, and the model fills it whether or not it searched.

6. **Answers to the same literature question are not stable.** ADAM17→ERBB4: "not a well-established direct substrate — ADAM10" (2026-04-30) vs "well-documented ADAM17 substrates" (2026-05-21), same user, same variant, same phrasing of the question (F24 vs G7). Neither message signals uncertainty. A single Perplexity summary decides the mechanistic story.

7. **Reconciliation with the loaded data happens for curation gaps but not for frequencies or nulls.** The model handled "ClinGen has no MFN2 entry" well (G4) and checked the user's gene annotations against nearest-gene output (G15). It did not notice that a 5%-frequency allele cannot be a VEO-IBD variant (F16), and it explained away a PheWAS null in favour of a small published study (F12). The instinct to reconcile exists only where the mismatch is a missing row, not where it is a contradiction of the paper.

8. **Where the model does scrutinize, it is when the question invites it.** G5, G8, G11–G13 all arise in messages whose user question was itself evaluative ("is there genetic evidence", "evaluate the connection", "are there any molecular QTL studies that implicate"). Gene-lookup "what do you know about X" prompts (F1, F6, F7, F17, F20) get the least-critical literature passes. The response format, not the backend, is the strongest predictor of whether a paper gets scrutinized.

## 6. Borderline cases skipped (8)

1. BIN2 (3c63b15c): "Richard et al. 2012, PLoS ONE (PMID 23285027)" — the Bin2 podosome paper is Sánchez-Barrena et al.; author attribution appears wrong but cannot be checked without the result.
2. PLCB2 (1e7f2e29): "Double knockout of PLCβ2/β3 abolishes GPCR responses in neutrophils" — mouse, species omitted; well-established fact, minor.
3. Leptin (5b0a3b13): the five leptin loci attributed to Hübel et al. 2021 rather than the 2016 leptin GWAS they were taken from — attribution nit.
4. deCODE (359f834d): the BPPV-vs-vertigo per-locus split cannot be checked; included only as provenance (F3), not as content.
5. TREC (273df12d): "TREC NBS adopted by 49 US states by 2017; 100% sensitivity for typical SCID" — review-derived numbers, plausible, not checkable.
6. OPA1 (a734c535): "DOA+ syndrome (~20% of cases)" — standard figure.
7. BMD–SCZ (f6c65720): "Many overlaps include the MHC region (chr17:45–46 Mb MAPT/KANSL1)" — MHC is chr6; a genetics slip, not a literature claim.
8. SCHEMA (6cd3a9b1 vs 143ad7bb): SCHEMA reported as 24,248/97,322 in one message and "SCHEMA2 (87,959 cases / 150,587 controls)" in the next without comment — loaded-data inconsistency, not literature.
