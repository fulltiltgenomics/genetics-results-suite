# Literature-evaluation review — transcript `finucane-0.txt`

Scope: all 10 sessions (user=`finucane`, 2026-04-09 → 2026-07-20), 4,907 lines, read in full. `lit_backend=perplexity` on **all 98** messages — the user never had `europepmc` selected.

## 1. Counts

| Item | Count |
|---|---:|
| `search_scientific_literature` calls, total | **27** |
| — requested `backend: perplexity` | 17 |
| — requested `backend: europepmc` | 7 |
| — no backend argument | 3 |
| — results **actually served by perplexity** (`"source": "perplexity"`) | **13 of 13** persisted results (100%), *including every europepmc request* |
| Calls with persisted RESULT | 13 |
| Calls with no persisted RESULT (judged from assistant text only) | 14 |
| `web_search` calls | 10 |
| Assistant messages reporting literature | 18 |
| — of those, with **no literature tool call in that turn** (memory / recycled citations) | **4** |

Structural fact that drives most findings: in **every** persisted literature result the per-paper records are empty — `"title": "", "authors": "", "journal": "", "year": "", "abstract": "", "doi": null, "pmid": null` — and the only content is a bare `url` list plus a free-text `summary`. The assistant nevertheless emits titles, authors, journals and years for these papers in 8 of 18 literature-reporting messages, pairing narrative text with positionally adjacent PMCIDs.

| Category | Findings tagged |
|---|---:|
| 1 — face-value pass-through | 8 |
| 2 — evidence-tier mismatch vs. hedged genetics | 5 |
| 3 — source does not support claim | 4 |
| 4 — Perplexity summary as primary source | 9 |
| 5 — candidate-gene / small-n as support | 3 |
| 6 — literature vs. loaded data unreconciled | 2 |
| 7 — missing / unverifiable provenance | 10 |
| 8 — review used as the evidence | 4 |
| **Findings total** | **12** |
| **Counter-examples** | **7** |
| User pushback on a literature claim | 1 |

Borderline skipped: **6** — three textbook-locus assertions from memory (CELSR2/SORT1–LDL, PTGER4–IBD, MC1R–pigmentation); the CDK13 elongation-mechanism relay (well hedged); the APOE-ε4/SCZ meta-analysis relay (n given, caveat attached); and the SCHEMA `24,248/97,322` vs loaded `SCHEMA2 87,959/150,587` divergence, probably a real freeze difference though never reconciled.

The complaint's example ("strong evidence of demyelination for schizophrenia … based on like 5 people") does **not** appear in this file.

## 2. Findings

- **[F1] session=19c38639 msg=146b55dc time=2026-06-18 18:04:55 user=finucane backend=perplexity**
- Question: are the SCZ/BIP genes associated with DD/ID also expressed earlier during development?
- Claim: "**Schizophrenia rare-variant/risk genes show prenatal expression enrichment**, peaking in the **early-to-mid-fetal prefrontal cortex** and in **developing glutamatergic neurons** of the second-trimester brain ([Perplexity backend summary; Willsey et al. *Cell* 2013, PMID 24267884]; [Miller/Sestan *Neuron* 2014, PMID 25465123])."
- Source (RESULT): "**Coexpression networks implicate human midfetal deep cortical projection neurons…** | **Authors:** Daniel H. Willsey, Gilad E. Willsey, et al. | *Cell* | 2013 … | **The Developmental Transcriptome of the Human Brain** | **Authors:** Andrew E. Miller, Daniel H. Willsey, Nenad Sestan, Flora M. Vaccarino, et al. | *Neuron* | 2014"
- Problem: 3, 4, 7 — the backend invented author lists (real paper is Willsey AJ et al.; "The Developmental Transcriptome of the Human Brain" is Kang et al. *Nature* 2011, not a 2014 *Neuron* paper by that list). The assistant copied the fabricated attribution and added PMIDs the RESULT does not contain (`"pmid": null` throughout). A critical reader would also have noticed a 2013 *autism* coexpression paper being cited for a schizophrenia prenatal-enrichment claim.
- Severity: high

- **[F2] session=19c38639 msg=146b55dc time=2026-06-18 18:04:55 backend=perplexity**
- Claim: "**schizophrenia is unique in showing BOTH prenatal and postnatal enrichment**, whereas **bipolar-disorder risk genes do NOT show a prenatal expression bias** … ([Perplexity backend; "Distinct effect of prenatal and postnatal brain expression across 20 complex traits," *Mol Psychiatry* 2021; PMC6362023])" — and as the conclusion: "the prenatal-vs-postnatal split … is most cleanly described as a **schizophrenia (prenatal) vs bipolar (postnatal)** distinction."
- Source (RESULT): "| **Prenatal Expression Bias** | **Strong positive correlation** with prefrontal cortex expression during **early midfetal development** and **early infancy** [2]. | **No evidence** of increased expression of risk genes in the **prenatal period** [2]. |"
- Problem: 1, 4, 7, 8 — the turn's load-bearing conclusion rests on one row of an AI-generated table whose reference `[2]` maps to a bare PMC URL with no title, author or year. No sample size, no stage definition, no effect size.
- Severity: high

- **[F3] session=49d626ae msg=ba1189c9 time=2026-06-17 18:54:42 backend=perplexity**
- Question: what is the evidence tying the variants within 1Mb to other genes, for the genes that are not SP4, STAG1, PPP3CA?
- Claim: "[INO80E increases risk of schizophrenia, Mol Psychiatry (PMC11957503)] — reports **INO80E** as a principal SCZ risk gene at 16p11.2" → verdict table: "literature favors **INO80E** as SCZ effector".
- Source (RESULT): "\"title\": \"\", \"authors\": \"\", … | **The INO80E at 16p11.2 locus increases risk of schizophrenia** | Not shown in the provided snippet | *Molecular Psychiatry* | 2025 | … | Not available in results |"
- Problem: 3, 4, 7, 8 — citation constructed from a narrative that itself says authors "Not shown" and DOI/PMID "Not available".
- Severity: medium

- **[F4] session=49d626ae msg=68f5aa64 time=2026-06-17 19:04:30 backend=perplexity**
- Question: tell me about INO80E. is that really plausible?
- Claim: "**Strongly supports plausibility — orthogonal functional validation.** The 2025 Mol Psychiatry mouse work moves this beyond statistical colocalization: overexpressing INO80E produced SCZ-like phenotypes."
- Source (RESULT): "overexpression in mouse mPFC produced schizophrenia-like behavioral, cellular, and molecular phenotypes.[1]"
- Problem: 1, 2, 4 — the tier mismatch in clearest form. Same message scrutinises loaded data to the decimal ("SCHEMA2 PTV burden is null (P≈0.67)… **LoF-tolerant** (pLI 0)") while a single mouse overexpression experiment relayed through an AI summary — no n, no line, no assays, no replication — becomes "**Strongly supports**".
- Severity: medium

- **[F5] session=de8ad14b msg=13a6cc0d time=2026-04-09 16:20:19 backend=perplexity**
- Question: what do you know about CACNA1D
- Claim: "A study (PMC10843263) found CACNA1D polymorphisms associate with systolic blood pressure and salt sensitivity … in Caucasians (~39% carry the risk allele), consistent with the FinnGen hypertension and CCB-use GWAS signals."
- Source: not persisted
- Problem: 2, 5, 6 — candidate-gene BP study relayed with no n, no replication, no note that pre-GWAS BP candidate-gene associations mostly do not replicate; then harmonised with the loaded signal. Same message hedges the loaded data properly ("AC=1–2 … hypothesis-generating only").
- Severity: medium

- **[F6] session=de8ad14b msg=13a6cc0d time=2026-04-09 16:20:19 backend=perplexity**
- Claim: "A 2024 *Neurology Genetics* paper (doi: 10.1212/NXG.0000000000200186) described a de novo variant c.3506G>A (p.G1169D) causing this syndrome with variable expressivity." … "CACNA1D somatic gain-of-function mutations account for ~9% of aldosterone-producing adenomas"
- Source: not persisted
- Problem: 1, 7 — a single clinical report stated as establishing causation with no patient count; the "~9%" has no cohort, denominator or citation.
- Severity: medium

- **[F7] session=793b0874 msg=66a8a294 time=2026-05-27 16:06:06 backend=perplexity**
- Question: does this include trans pQTLs? can you restrict to cis pQTLs?
- Claim: "[Morita et al. 2004 (Mol Cell Biol)] showed that **LGR5 knockout mice develop ankyloglossia** … The cis-pQTL colocalization shown here is, to my knowledge, the **first human genetic evidence linking LGR5 to ankyloglossia**, recapitulating the well-established mouse phenotype"
- Source: not persisted
- Problem: 1, 4 — one 2004 single-knockout-line paper upgraded to "the well-established mouse phenotype" and used to license a novelty claim, while the human side carries the assistant's own SomaScan cross-reactivity warning two paragraphs later.
- Severity: medium

- **[F8] session=19c38639 msg=35ff8f16 time=2026-06-18 18:13:38 backend=perplexity**
- Question: look in a couple of standard PPI databases for interactions among these
- Claim: "Across multiple primary papers, AKAP11/AKAP220 partners are **PKA (RIα/RIβ), GSK3β, PP1, IQGAP1, VAPA/VAPB, DYRK1A, SPHKAP, ANXA1, SQSTM1/p62, LC3/GABARAP** … ([Tanji et al. *JBC* 2002, PMC4528109]; …)"
- Source (RESULT): "…Tanji, Yamamoto, … *JBC* — **2002** … **DOI/PMID:** not provided in the search results." — while the URL list's first entry `PMC4528109` is assigned by the summary's own numbering to a different, 2015 paper.
- Problem: 3, 4, 7 — the PMCID→paper mapping is almost certainly wrong, produced by list position. "Across multiple primary papers" overstates: no primary paper was opened.
- Severity: medium

- **[F9] session=51f3467e msg=d8148c14 time=2026-06-25 17:39:55 backend=perplexity**
- Question: is there a way to tell whether CDK13 effects are all in early development, making an inhibitor safer for adults?
- Claim: "| **YJ1206 / YJ9069** ([PMC11513842]) | oral CDK12/13 degrader | "Well tolerated," <10–20% body-weight loss; minimal liver/kidney/prostate toxicity; tolerated long-term in immunocompetent CD-1 mice |" → "**adult pharmacology is cautiously encouraging**".
- Source (RESULT): "the most relevant scientific paper is … (likely **PMCID: 11513842**), which details a CDK12/13 degrader/inhibitor (compound **YJ1206**, reused as **YJ9069** in some contexts)"
- Problem: 1, 4, 7 — backend uncertain about both paper and compound identity; collapsed into a clean tolerability row feeding a drug-safety inference.
- Severity: medium-high (safety-relevant)

- **[F10] session=51f3467e msg=9a571bc6 time=2026-06-25 17:26:35 backend=perplexity**
- Claim: "Activity demonstrated in TNBC, HGSOC, and glioblastoma ([*Nat Cancer* 2026](https://doi.org/10.1038/s44321-026-00393-w); …)" … "**YJZ5118** (covalent, IC₅₀ 26 nM CDK13)"
- Source: not persisted
- Problem: 3, 7 — DOI prefix `10.1038/s44321-…` is *EMBO Molecular Medicine*, not *Nature Cancer*; IC₅₀ quoted with no assay format or CDK12 selectivity ratio.
- Severity: medium

- **[F11] session=08a009ca msg=369a2255 time=2026-06-29 18:00:33 backend=perplexity**
- Question: what can you tell me about SP4?
- Claim: "| [Mol Psychiatry 2010 (PMC3202296)] | **SP4 protein reduced** in postmortem cerebellum/PFC of **bipolar** subjects |" and "| [J Pers Med 2024] | SP4 mRNA upregulated in buccal swabs of SCZ patients |"
- Source: not persisted
- Problem: 1, 2, 5 — a 2010 postmortem protein case/control study and a buccal-swab mRNA study tabulated with no n, no confounds, no replication status. Same message filters loaded exome data rigorously.
- Severity: medium

- **[F12] session=19c38639 msg=6f5dd3d5 time=2026-06-18 17:59:42 backend=none (no literature call)**
- Claim: "HDAC9 (well-established large-vessel **ischemic stroke** locus; p≈5e-18 region) … The HDAC9–stroke association is textbook and is a positive control rather than a new finding."
- Source: "I was unable to run the literature-search subagents (service not initialized) and have not performed targeted literature searches in this pass."
- Problem: 1, 7 — a specific p-value produced from memory in a message that states no search was run.
- Severity: medium

## 3. Counter-examples

- **[G1] session=e1dfe126 msg=b5bbcc54 time=2026-07-17 17:34:03** — "meta-analyses (28 studies, 3,452 cases) find **no association between APOE and schizophrenia susceptibility** … These are `perplexity`-sourced summaries and I have not independently verified each primary paper." Uses the literature to overturn an apparently strong loaded-data signal.
- **[G2] session=793b0874 msg=11867ecb time=2026-05-27 16:13:34** — "Reported associations with **schizophrenia, bipolar disorder, addiction, cerebellar atrophy/ID** in candidate-gene and case-report literature (largely unreplicated, weak evidence)".
- **[G3] session=49d626ae msg=68f5aa64 time=2026-06-17 19:04:30** — "**Important caveat — INO80E is NOT a rare-variant (SCHEMA) gene.** … a different and somewhat weaker class of causal inference"; came **after** user challenge.
- **[G4] session=49d626ae msg=ba1189c9** — "The perplexity search returned **no paper that cleanly resolves the causal SCZ gene** at this locus … only tangential preprints."
- **[G5] session=19c38639 msg=35ff8f16** — "I do **not** have a programmatic connector to STRING or BioGRID … don't treat the synthesis below as a direct database dump."
- **[G6] session=8c497129 msg=62628630** — "Lehalle D, … *J Med Genet* 2017. DOI 10.1136/jmedgenet-2016-104468. **17 patients**"; the only message reporting a cohort size for a clinical-genetics paper.
- **[G7] session=49d626ae msg=ba495357** — "the summary above is AI-generated by the perplexity backend; I have not independently verified every claim".

## 4. User pushback

One instance: session=49d626ae msg=f97667b9 — after "literature favors **INO80E** as SCZ effector" (F3), the user asked "tell me about INO80E. is that really plausible?" The follow-up (G3) is materially more rigorous. All other pushback is on loaded data. **The data side has an adversarial reviewer; the literature side does not.**

## 5. Patterns

1. The backend is a single point of failure. Seven calls explicitly requested `europepmc`; all were served by perplexity. The assistant noticed four times but never retried or discounted.
2. The mechanical root cause is empty structured fields filled in from prose. Every persisted record is `"title": "", … "pmid": null` plus a URL list and a `summary`. Highest-yield fix: if the record has no title/PMID, cite the URL and nothing else.
3. No literature-evidence rubric; stark asymmetry with the genetics rubric. Sample size appears for a clinical paper exactly once in 18 messages.
4. Perplexity's hedges are stripped in transmission; its assertions are not.
5. The three-pass format protects the data and exposes the literature. A blanket caveat does not de-rate a specific claim in a table.
6. Literature is cited from memory in 4 of 18 messages with no marker.
7. Scrutiny is reactive, not default — triggered by user challenge or a too-good result.
