# Literature-evaluation review: finucane-1.txt

Scope: 4 sessions, 17 assistant messages (all `lit_backend=perplexity`), user `finucane`, 2026-07-27 to 2026-09-09. Every line of the 2,372-line file was read.

## 1. Counts

| Item | Count |
|---|---|
| `search_scientific_literature` calls | 18 (17 with a persisted RESULT; 1 legacy call in msg 3dffe377 with no RESULT) |
| — backend perplexity | 18 (all; every record in every RESULT carries `"source": "perplexity"`) |
| — backend europepmc | 0 |
| `web_search` calls | 0 |
| Records returned across persisted RESULTs | 99: 60 resolved to a Europe PMC record (PMID/DOI), 38 Perplexity-only snippets (no PMID/DOI; includes bibliography fragments, figure pages, a supplement PDF), 16 flagged `is_preprint: true` |
| Perplexity `summary` field persisted | 1 of 17 RESULTs (the S1PR4 call in msg 1473f332) |
| Assistant messages that report literature | 11 of 17 |
| Messages citing literature with **no** tool call | 1 (msg d7142d16, Udler et al. from memory). Msg 07e3a7cf cites "Trubetskoy et al. 2022 (ST11a)" but that is the dataset description, not a literature claim — not counted |
| Findings | 16 (0 high, 5 medium, 11 low) |
| — cat 1 face-value pass-through | 4 |
| — cat 2 evidence-tier mismatch | 1 |
| — cat 3 source does not support claim | 7 |
| — cat 4 Perplexity summary as primary source | 1 |
| — cat 5 old candidate-gene / small-n as support | 1 |
| — cat 6 literature vs loaded data unreconciled | 1 |
| — cat 7 missing / blended provenance | 9 |
| — cat 8 review / consensus taken as evidence | 2 |
| Counter-examples | 14 |
| User pushback on a literature claim | 0 (two user challenges — msgs 197d4b3e "how are you currently predicting peak-gene links?" and e6dc23c5 "does flare assign a direction" — target the assistant's *data* claims; both produced explicit corrections) |
| Borderline cases skipped | ~15 citations in 4 classes (see end of section 2) |

(Categories overlap, so the per-category column sums to more than 16.)

## 2. Findings

- **[F1] session=a04b73d4 msg=3dffe377-3501-496e-b167-cef13fbc6bbf time=2026-07-27 16:52:54 user=finucane backend=perplexity**
- Question: what genes have the most significant missense-only-burden association to schizophrenia, but no LoF-burden? of these, are any GPCRs?
- Claim (verbatim): "[Dean et al. 2023, *Front Cell Neurosci*] Pre-clinical, post-mortem and neuroimaging evidence for M1/M4 in SCZ pathology; two clinical trials show M1/M4 activation reduces positive, negative and cognitive symptoms." … "M4 is the target of xanomeline–trospium (KarXT), an approved schizophrenia therapeutic"
- Source: not persisted (legacy message). The same review's abstract, returned later in msg 8867b7c4, reads "This hypothesis is now supported by results from two clinical trials which indicate that activating central muscarinic M1 and M4 receptors can reduce the severity of positive, negative and cognitive symptoms"
- Problem: 8, 7 — a narrative review's one-sentence summary of two trials is relayed as the trial evidence; the trials are never named, sized or cited, and the approval claim is from memory with no citation, in a table headed "Backend queried: perplexity".
- Severity: low

- **[F2] session=a04b73d4 msg=1473f332-8df7-4a91-ac24-755ee252a9c6 time=2026-07-27 16:58:52 user=finucane backend=perplexity**
- Question: how about non-scz traits in genebass? which GPCRs have an overall missense burden but no ptv burden?
- Claim (verbatim): "[CHARGE Hematology Working Group 2016, *Nat Genet*] Discovery of **S1PR4** for neutrophil count: rare missense p.Arg365Leu, combined discovery+replication p = 5×10⁻¹², MAF ≈ 0.006."
- Source (verbatim from RESULT): paper abstract: "The association of a rare missense variant in S1PR4 supports the role of sphingosine-1-phosphate signaling"; Perplexity `summary`: "The rare **S1PR4** missense variant **p.Arg365Leu (rs3746072)** was associated with **lower total WBC** … combined discovery+replication significance reported as \(p = 5 \times..."
- Problem: 4 — the variant name and p-value come from the AI-generated summary, not from the paper record; "MAF ≈ 0.006" appears nowhere in the result. Relayed as the paper's numbers.
- Severity: low

- **[F3] session=a04b73d4 msg=1473f332-8df7-4a91-ac24-755ee252a9c6 time=2026-07-27 16:58:52 user=finucane backend=perplexity**
- Question: (as F2)
- Claim (verbatim): "[Burg et al. 2022, *Nat Rev Rheumatol*] S1PR-targeted drugs approved in MS and IBD; notes S1PR4 polymorphisms associated with circulating neutrophil numbers."
- Source (verbatim from RESULT): "Several drugs that target S1PRs have been approved for the treatment of multiple sclerosis and inflammatory bowel disease and are under clinical testing for patients with systemic lupus erythematosus (SLE)." — no mention of S1PR4, polymorphisms or neutrophils anywhere in the returned abstract.
- Problem: 3 — the second half of the row is not in the returned content.
- Severity: low

- **[F4] session=1c826fec msg=2120399f-75d4-42b1-95c3-f59411f3fbcc time=2026-07-29 18:08:04 user=finucane backend=perplexity**
- Question: using pseudo-credible sets, are there any cell types (brain or not brain - test all of them) that are enriched for SCZ credible set variants?
- Claim (verbatim): "It is consistent with the MHC signal acting through neuronally-expressed *C4A* rather than through immune cells — though I have not tested *C4A* directly here, so that mechanistic link is an inference from the literature, not from this analysis."
- Source: no C4A paper in the RESULT; none cited.
- Problem: 7 — a mechanistic claim is explicitly attributed to "the literature" but no paper (Sekar et al. 2016 or otherwise) is given, so the reader cannot check it. The self-flag is good; the missing citation is the gap.
- Severity: low

- **[F5] session=1c826fec msg=8867b7c4-dc22-4552-96d2-14f740f5376f time=2026-07-29 18:32:51 user=finucane backend=perplexity**
- Question: are there any pseudo-credible sets that fall near RVAS genes for SCZ?
- Claim (verbatim): "[Dean et al. 2023] … **notes that strong GWAS-level association has not been established for CHRM4** across large SCZ GWAS." and in Pass 3: "Dean et al. 2023 state directly that strong GWAS-level association has **not** been established for CHRM4"
- Source (verbatim from RESULT): "Pre-clinical models, postmortem and neuroimaging studies all support a role for muscarinic receptors in the molecular pathology of schizophrenia. … This review will provide an update on a growing body of evidence" — the returned abstract contains no reference to GWAS at all (the only "GWAS" hits in that RESULT are in the Lencz & Malhotra 2015 record).
- Problem: 3 — "state directly" attributes to a review a statement absent from the returned content; the confidence escalates from "notes" (table) to "state directly" (analysis) within the same message. The loaded data in the same message show the CHRM4 locus at p = 3.7e-16, so the sentence is also doing work against the assistant's own data without a checkable source.
- Severity: medium

- **[F6] session=1c826fec msg=8867b7c4-dc22-4552-96d2-14f740f5376f time=2026-07-29 18:32:51 user=finucane backend=perplexity**
- Question: (as F5)
- Claim (verbatim): "[Kamp et al. 2025, *J Psychiatr Res*] SP4 is 'one of the two schizophrenia-risk genes shared between the SCHEMA and the GWAS.'" then "Trubetskoy et al. named exactly **'GRIN2A, SP4, STAG1, and FAM120A'** as the convergence genes, and Kamp et al. 2025 call SP4 one of only two genes shared between SCHEMA and the GWAS."
- Source (verbatim from RESULT): Kamp abstract: "Human SP4 gene is also one of the 69 schizophrenia-risk genes prioritized by parallel genome-wide association studies (GWAS) and one of the two schizophrenia-risk genes shared between the SCHEMA and the GWAS"
- Problem: 1, 8 — the introductory framing of a mouse AAV-rescue paper is repeated as fact, immediately next to the primary GWAS source naming four genes; the two-versus-four discrepancy is not noticed or reconciled.
- Severity: low

- **[F7] session=1c826fec msg=8867b7c4-dc22-4552-96d2-14f740f5376f time=2026-07-29 18:32:51 user=finucane backend=perplexity**
- Question: (as F5)
- Claim (verbatim): "**5. SP4 has the deepest mechanistic support of any gene here, from three independent layers.** The SCZ CS shares variants with an *SP4* eQTL in **brain (cerebellum)** …; MGI records **reduced sensorimotor gating** …; and Zhou 2022 reports a truncation OR of 9.37."
- Source (verbatim from RESULT): Zhou 2022 abstract: "Truncation of the human SP4 gene has an odds ratio of 9.37 (3.38-29.7) for schizophrenia."
- Problem: 7, 6 — the OR is SCHEMA's (Singh et al. 2022), relayed by a single-author bioinformatic commentary; presenting it as "Zhou 2022 reports" and as an "independent layer" double-counts the SP4 burden signal that is already in the loaded data (`schema2` SP4 PTV_missense p = 8.9e-9, Pass 1B of the same message).
- Severity: low

- **[F8] session=1c826fec msg=53bd1dbf-ca47-4c9a-a2ef-4b8c2070be82 time=2026-07-29 18:49:02 user=finucane backend=perplexity**
- Question: do the variants in SP4 STAG1 credible sets overlap chromatin accessibility peaks in enriched tissues? how about FAM120A and GRIN2A? also, can you tell me more about the sQTLs?
- Claim (verbatim): "[Hall & Bray 2022, *Biol Psychiatry*] **SP4 regulates NMDA receptor subunit expression** — a direct SP4→GRIN2A transcriptional link." and "Hall & Bray 2022 note SP4 itself regulates NMDA receptor subunit expression — a plausible SP4→GRIN2A axis linking two of the four genes."
- Source: Hall & Bray was not returned by either search in this message. Its abstract, returned in the previous message (line 957), reads "These data provide strong support for the view that synaptic dysfunction within glutamatergic and GABAergic … neurons … is a central component of schizophrenia pathophysiology" — no mention of SP4 or NMDA subunits.
- Problem: 3, 7 — a specific gene→gene mechanism is attributed to a review whose returned content does not contain it; the Sp4 mouse literature concerns Grin1/NR1, and the leap to GRIN2A specifically is the model's. This creates an "axis linking two of the four genes" the user could reasonably pursue.
- Severity: medium

- **[F9] session=1c826fec msg=53bd1dbf-ca47-4c9a-a2ef-4b8c2070be82 time=2026-07-29 18:49:02 user=finucane backend=perplexity**
- Question: (as F8)
- Claim (verbatim): "[Zhang Q et al. 2022, *Psychiatry Res*] '**Common and rare variants within SP4 exert distinct molecular mechanisms**' — explicitly argues the two signal classes act differently." and "**8. The literature independently predicts the SP4 result, which reframes the negative.** Zhang Q et al. 2022 argue that common and rare SP4 variants 'exert **distinct** molecular mechanisms,'"
- Source (verbatim from RESULT): `"abstract": "# Common and rare variants within SP4 exert distinct molecular mechanisms contributing to the risk of schizophrenia\nPsychiatry Res.\n2022 Dec:318:114948."` — title and journal only; no abstract, design or sample size returned.
- Problem: 1, 3 — a paper known only by its title is used to "reframe" a null chromatin result as expected; nothing about what the paper did was available.
- Severity: medium

- **[F10] session=1c826fec msg=1638b52a-43cb-4afd-b140-03ba56ca593e time=2026-07-29 19:04:43 user=finucane backend=perplexity**
- Question: how are you currently predicting peak-gene links? … are there any pseudo credible set variants that chrombpnet and/or MPRA data predicts to affect open chromatin?
- Claim (verbatim): "[Marderstein et al., ChromBPNet + FLARE framework] … ChromBPNet predicted allele effects for **~15 million variants across 132 cellular contexts** (~2 billion predictions); **FLARE integrates those predictions with evolutionary signals to prioritize RARE variants**" and "**5. The literature explains why, and it means this is by design.** The Marderstein framework paper is titled 'Mapping the regulatory effects of common and **rare** non-coding variants,'"
- Source (verbatim from RESULT): `"title": "Mapping the regulatory effects of common and rare non ... - PMC"`, `"abstract": "Here, we present a novel framework for analyzing regulatory effects of non-coding variants, consisting of ChromBPNet and FLARE.\nChromBPNet is a recently developed convolutional neural network model..."` — no variant counts, no "evolutionary", no rare-variant purpose in the returned snippet.
- Problem: 3, 7 — the numbers and the stated purpose come from outside the returned content, and a title that says "common and rare" is read with "rare" bolded to explain a zero-overlap coverage gap as the resource's design rather than as a possible ingestion problem. The assistant's own join checks are sound; the literature justification is not.
- Severity: medium

- **[F11] session=d2370363 msg=486a4b95-b740-4978-8221-b26f7e423ef3 time=2026-09-09 19:58:08 user=finucane backend=perplexity**
- Question: using pgc_scz_finemap data and your epigenetic data, (a) are high-PIP variants enriched for open chromatin peaks in any cell types? (b) … list of genes … closest gene to a high-PIP variant that falls in a peak in at least one neuronal cell type
- Claim (verbatim): "for that you want stratified LDSC on full summary stats, which is what [Trubetskoy et al. 2022] and [Hook & McCallion 2020] did (both find excitatory/inhibitory neuron enrichment; layer V strongest)."
- Source (verbatim from RESULT): Hook & McCallion: "we use open chromatin profiles from discrete mouse cell populations … mouse-derived human open chromatin profiles can serve as powerful proxies … maximal enrichment in cortical layer V excitatory neurons"; Trubetskoy: "Associations were concentrated in genes that are expressed in excitatory and inhibitory neurons" (no layer result; expression-based, not chromatin S-LDSC).
- Problem: 1, 3 — the layer-V result rests on mouse chromatin used as a proxy for human cell types, which is not mentioned; the parenthetical attributes it to both papers.
- Severity: low

- **[F12] session=d2370363 msg=7633a679-dfa3-48d3-8f25-6a74db55ff47 time=2026-09-09 20:24:42 user=finucane backend=perplexity**
- Question: check both: what is the direction of effect on SCZ, and can you do a PheWAS.
- Claim (verbatim): "That is the direction the functional literature reports. [Hou et al. 2018] show the G allele creates a miR-338-3p site in the FURIN 3′UTR, driving allele-specific FURIN downregulation and reduced mature BDNF; [Yang et al. 2022] independently show rs4702-A associates with higher FURIN and BDNF; reviewed in [Zhang et al. 2022], which notes reduced FURIN mRNA in schizophrenia brain."
- Source (verbatim from RESULT): Hou: "Allele-specific downregulation of FURIN by miR-338-3p was validated with a luciferase reporter assay. … reduced brain-derived neurotrophic factor (BDNF) maturation and secretion in human embryonic kidney 293T cells." Yang: "We grouped the 106 glioma patients recruited according to the rs4702 polymorphism. … rs4702-A was associated with increased expression of FURIN and BDNF in the serum and PBMC of glioma patients after radiotherapy. … increased enterotype I"
- Problem: 1, 2, 5 — a HEK293T luciferase assay and a 106-patient post-radiotherapy glioma study (serum/PBMC, with gut-flora endpoints) are relayed as "the functional literature" that "independently show[s]" direction, with no cell line, n or design given. In the same message the loaded data are hedged carefully ("those are **pseudo** credible sets", "I have not run colocalization to confirm that"). The direction conclusion is actually carried by the loaded DLPFC eQTL (PIP ≈ 1), but the reader is told the literature carries it.
- Severity: medium

- **[F13] session=a43836bd msg=d7142d16-a1eb-4fc3-bd99-299b518adc80 time=2026-09-09 20:28:17 user=finucane backend=none**
- Question: can you take the top snp from each SCZ locus, and do a PheWAS, and then NMF the resulting loci x phenotypes matrix, analogous to the Udler et al T2D analysis?
- Claim (verbatim): "Udler et al. used *full summary-statistic z-scores* for every variant × every trait (a dense ~50×47 matrix), not credible-set membership."
- Source: no tool call; no citation, PMID or link.
- Problem: 7, 3 — from memory, uncited; Udler et al. 2018 (PLoS Med) used 94 T2D variants × 47 traits, so "~50" is wrong. The sparsity conclusion built on it is still right.
- Severity: low

- **[F14] session=a04b73d4 msg=3dffe377-3501-496e-b167-cef13fbc6bbf time=2026-07-27 16:52:54 user=finucane backend=perplexity**
- Question: (as F1)
- Claim (verbatim): "[Mol Psychiatry 2022 — ultra-rare + common variant convergence] Cases enriched for ultra-rare (β=0.079, P=1.8×10⁻⁵) and rare PTVs; **synonymous not associated** (P=0.93)" reused as a calibration point: "[Mol Psychiatry 2022] confirms it is (P=0.93 in a properly powered analysis)". Also "[Nature Genetics 2023 — PTV risk conserved across populations] Confirms rare-PTV enrichment in constrained genes across ancestries."
- Source: not persisted.
- Problem: 7 — two papers cited with no authors, no PMID and no sample size, one of whose numbers is then used to judge the loaded Genebass scan as noise.
- Severity: low

- **[F15] session=1c826fec msg=8867b7c4-dc22-4552-96d2-14f740f5376f time=2026-07-29 18:32:51 user=finucane backend=perplexity**
- Question: (as F5)
- Claim (verbatim): "[Nature Commun 2023, brain structure–SCZ](https://www.nature.com/articles/s41467-023-43567-7) | chr11p11 harbours *CHRM4, MDK, AMBRA1, HARBI1* — a gene-dense region where causal-gene assignment is ambiguous."
- Source: the URL does not appear in any persisted RESULT for this message (three searches, each truncated at 8–12 records).
- Problem: 7 — no authors, no PMID, and the paper cannot be located in the returned results; the claim is generic locus lore rather than a finding of that paper.
- Severity: low

- **[F16] session=1c826fec msg=1638b52a-43cb-4afd-b140-03ba56ca593e time=2026-07-29 19:04:43 user=finucane backend=perplexity**
- Question: (as F10)
- Claim (verbatim): "It sits in the chr10q24 *AS3MT/BORCS7/NT5C2* region, one of the best-established SCZ loci." and "That region is a well-known polymorphic inversion where LD is extreme and fine-mapping fails"
- Source: neither locus is in the RESULTs; no citation.
- Problem: 7 — correct, but uncited memory statements sit inside a message whose literature table is otherwise fully linked, so the reader cannot separate searched from recalled.
- Severity: low

### Borderline cases skipped (not filed)

1. Citations that do not appear in the persisted RESULT text but may sit beyond truncation (results record e.g. `"returned": 12` while ~8 records are persisted): Klimiankou 2024, Campbell 2021 (msg 1473f332); Santos 2006, Ingham 2016 (with "n = 6,099"), Mummidi 1998 ("n = 1,090"), Al-Abdulhadi 2010 ("154 families") (msg fc2759f1); Tansey & Hill 2018 (with p = 3.13×10⁻⁴ / 0.470), Liu 2022, Maserrat & Cairns 2025 (msg 2120399f); Kraft 2026, Akingbuwa 2022 (msg 8867b7c4); Hervoso 2024 details, Mulvey 2021 (msgs 53bd1dbf, 1638b52a). Absence from a truncated persistence is not evidence of fabrication, so these are not filed; they are the reason category 3 is under-countable in this file.
2. Uncited textbook facts in an otherwise-cited message: "furin substrates include TGF-β and pro-endothelin" (msg 4dd894f5); "glutamatergic-neuron specificity for SCZ has been the field's consensus since roughly 2018" (msg 2120399f, backed by three cited papers in the same table).
3. Reviews cited but with no claim built on them: Lim 2026 (msg 3dffe377), Rosenkilde 2025 and Campbell 2021 (msg 1473f332), Nakamura & Takata 2023 (msg 8867b7c4).
4. Dataset-provenance citations ("Trubetskoy 2022 ST11a", "Li 2023 cCRE→gene links", "Siraj et al. tested fine-mapped GTEx/UKBB/BBJ variants") that come from dataset descriptions rather than a literature claim.

## 3. Counter-examples

- **[G1] session=a04b73d4 msg=3dffe377-3501-496e-b167-cef13fbc6bbf time=2026-07-27 16:52:54 user=finucane backend=perplexity**
- Question: (as F1)
- Claim (verbatim): "[Scarr et al. 2013] CHRM4 sequencing in 76 cases / 74 controls; rs2067482 nominal association (p=0.03–0.05). **Very small, candidate-gene design.**" / "[Pozhidaev et al. 2020] 449 cases / 134 controls; … **direction is opposite** to Scarr et al. Unreplicated, conflicting."
- Source: not persisted (abstracts returned later in msg 8867b7c4 confirm both n's and the opposite risk alleles).
- Scrutiny: n, design, replication status and allele-direction conflict all stated; then used correctly: "the SCHEMA2 rare-variant signal is a much better-powered but still sub-threshold piece of evidence, not a replication of those studies."
- Severity: n/a (baseline good behaviour)

- **[G2] session=a04b73d4 msg=3dffe377-3501-496e-b167-cef13fbc6bbf time=2026-07-27 16:52:54 user=finucane backend=perplexity**
- Question: (as F1)
- Claim (verbatim): "[Purcell et al. 2014, *Nature*] Early SCZ exome study: polygenic burden of rare disruptive mutations; **no individual gene reached significance** — a power cautionary tale."
- Source: not persisted.
- Scrutiny: a landmark paper read for what it did *not* find and applied to the loaded Genebass scan's power problem.

- **[G3] session=a04b73d4 msg=fc2759f1-a0d6-4c0d-b111-a2ea7f4d23ff time=2026-07-27 17:32:51 user=finucane backend=perplexity**
- Question: Are S1PR4, S1PR2, CCR3, or CCR2 associated with any complex or rare diseases?
- Claim (verbatim): "[Easterbrook et al. 1999] **CCR2B-64I (rs1799864) associated with slower HIV progression** … HR 0.39, 95% CI 0.17–0.91; n = 132 — very small." / "[Al-Abdulhadi & Al-Rabia 2010] 154 families … Small, candidate-gene design." / "The classical CCR2-64I/HIV literature is weak by modern standards"
- Source (verbatim from RESULT): Easterbrook abstract: "studied in 132 white HIV type 1 (HIV-1)-infected participants … hazards ratio [HR], 0.39; 95% confidence interval [CI], 0.17-0.91"
- Scrutiny: n and CI carried through, ancestry non-replication named, and the whole line down-weighted in the conclusion.

- **[G4] session=a04b73d4 msg=fc2759f1-a0d6-4c0d-b111-a2ea7f4d23ff time=2026-07-27 17:32:51 user=finucane backend=perplexity**
- Question: (as G3)
- Claim (verbatim): "[Hong et al. 2022] S1PR4 expression 6-fold higher in NASH patients vs controls; *S1pr4*⁺/⁻ mice protected … **Expression/mouse evidence, not human genetic association.**" / "[Huang et al. 2023, *iScience*] … (expression-based, not germline genetics)." / "[Nature 2022 (Cell Discovery)] … 'well-established target for allergic diseases such as asthma' — pharmacology, not human genetics."
- Source (verbatim from RESULT): Hong abstract is mouse/expression; Cell Discovery snippet: "The activation of CCR3 is associated with chemotaxis of eosinophils, thus considered as a well-established target for allergic diseases, such as asthma"
- Scrutiny: evidence type labelled on every non-genetic paper, so none is mistaken for a disease association.

- **[G5] session=a04b73d4 msg=fc2759f1-a0d6-4c0d-b111-a2ea7f4d23ff time=2026-07-27 17:32:51 user=finucane backend=perplexity**
- Question: (as G3)
- Claim (verbatim): "This association is *not* in the S1PR4 literature I found — [CHARGE 2016] reported S1PR4 for neutrophil count … I have not verified whether this thyroid signal is already reported; treat it as an interesting but unconfirmed-novelty finding requiring non-Finnish replication."
- Source: three S1PR4 searches, none returning a thyroid paper.
- Scrutiny: absence from a search is not treated as proof of novelty; the loaded FinnGen finding is labelled unconfirmed rather than "novel".

- **[G6] session=1c826fec msg=2120399f-75d4-42b1-95c3-f59411f3fbcc time=2026-07-29 18:08:04 user=finucane backend=perplexity**
- Question: (as F4)
- Claim (verbatim): "[Heritability enrichment in open chromatin… (bioRxiv 427484, 2018)]" / "**This is a positive control recovering a well-established finding, not a discovery**" / "the ORs are not comparable to published heritability enrichment folds (e.g. Bryois et al.'s 7.1-fold)."
- Source (verbatim from RESULT): `"is_preprint": true, "url": "https://www.biorxiv.org/content/10.1101/427484v1.full.pdf"`; Bryois abstract: "ATAC-seq on adult prefrontal cortex brain samples from 135 individuals with schizophrenia and 137 controls"
- Scrutiny: preprint labelled as such; own result framed as replication not discovery; refuses to compare an overlap OR with a published S-LDSC fold; Bryois n given.

- **[G7] session=1c826fec msg=8867b7c4-dc22-4552-96d2-14f740f5376f time=2026-07-29 18:32:51 user=finucane backend=perplexity**
- Question: (as F5)
- Claim (verbatim): "[Trubetskoy et al., PGC3 GWAS (medRxiv preprint text)]" / "Candidate-gene CHRM4 rs2067482 association studies — **small (76/74 and 449/134) and directionally inconsistent** (C allele risk in 2013, T allele risk in 2020). Weak, unreplicated evidence." / "No literature returned by my searches names PCNX3 as a SCZ convergence gene … so I have not verified this is novel"
- Source (verbatim from RESULT): Scarr: "sequencing the CHRM4 gene from the brains of 76 people with the disorder and 74 people"; Pozhidaev: "449 patients with schizophrenia and 134 healthy controls"
- Scrutiny: preprint flagged, candidate-gene studies sized and contradicted, and a possibly-novel gene explicitly left unverified.

- **[G8] session=1c826fec msg=53bd1dbf-ca47-4c9a-a2ef-4b8c2070be82 time=2026-07-29 18:49:02 user=finucane backend=perplexity**
- Question: (as F8)
- Claim (verbatim): "Chick et al. 2025 … concluding for 'an aetiological role for **disrupted chromatin organisation**' — STAG1 being a cohesin subunit. That is a claim about STAG1 *organising* chromatin, which is a different proposition from the SCZ risk variant lying *within* accessible chromatin, and my data speak only to the latter."
- Source (verbatim from RESULT): Chick abstract: "further evidence suggesting an aetiological role for disrupted chromatin organisation"
- Scrutiny: a phrase that superficially matches the question is read precisely and kept apart from the assistant's own chromatin-overlap result; also "[Chick et al. 2024 preprint] Same result" labelled as preprint.

- **[G9] session=1c826fec msg=53bd1dbf-ca47-4c9a-a2ef-4b8c2070be82 time=2026-07-29 18:49:02 user=finucane backend=perplexity**
- Question: (as F8)
- Claim (verbatim): "The sQTL data are consistent with the field's framing but do not, on their own, establish causality. Takata et al. 2017 … Hervoso et al. 2024 argue splicing effects may exceed expression effects — so looking here is well motivated. But variant sharing and r² = 0.68 are **not colocalization**."
- Source (verbatim from RESULT): Takata appears only as a bibliography fragment: "24.  A. Takata, N. Matsumoto, T. Kato, Genome-wide identification of splicing QTLs in the human brain and their enrichment among schizophrenia-associated loci."
- Scrutiny: literature used as motivation, not as evidence for the specific locus; a bibliography fragment was turned into a proper DOI citation rather than quoted as a finding.

- **[G10] session=1c826fec msg=1638b52a-43cb-4afd-b140-03ba56ca593e time=2026-07-29 19:04:43 user=finucane backend=perplexity**
- Question: (as F10)
- Claim (verbatim): "[McAfee et al. 2023, *Cell Genomics*] MPRA on **5,173 fine-mapped SCZ GWAS variants** in primary human neural progenitors → **439 MPRA-positive**. Critically: 'Transcription factor binding had modest predictive power, while **fine-map posterior probability, enhancer overlap, and evolutionary conservation failed to predict MPRA-positive variants**.'"
- Source (verbatim from RESULT): "We performed a massively parallel reporter assay (MPRA) on 5,173 fine-mapped schizophrenia GWAS variants in primary human neural progenitors and identified 439 variants with allelic regulatory effects"
- Scrutiny: design, cell system and counts carried verbatim; then used to interpret the assistant's own null (PIP does not predict emVar) as "consistent with the best available SCZ-specific MPRA study — it is not obviously an artifact". Also "[Myint et al. 2018, bioRxiv]" labelled as preprint.

- **[G11] session=d2370363 msg=486a4b95-b740-4978-8221-b26f7e423ef3 time=2026-09-09 19:58:08 user=finucane backend=perplexity**
- Question: (as F11)
- Claim (verbatim): "So I would not read the brain ranking as a cell-type enrichment test — for that you want stratified LDSC on full summary stats, which is what [Trubetskoy et al. 2022] and [Hook & McCallion 2020] did"
- Source: as F11.
- Scrutiny: the assistant's own overlap test is subordinated to the published method rather than the reverse (the mouse-proxy omission in F11 notwithstanding).
  - VOID 2026-09-27: the praised sentence is the one F11 flags with the same diagnosis, and the full record confirms F11 — Trubetskoy 2022's record describes gene-expression concentration, not stratified LDSC, and only the mouse Hook & McCallion paper has layer V.

- **[G12] session=d2370363 msg=4dd894f5-9b2c-49ce-b55b-62ca9245ccf3 time=2026-09-09 20:22:14 user=finucane backend=perplexity**
- Question: do you have any pQTL data where you could check whether 15:90883330:G:A is a pQTL for any genes?
- Claim (verbatim): "these are Olink antibody measurements — a change in the prodomain/mature ratio can shift epitope availability, so 'more protein' and 'more cleavage' are not distinguishable here. HRC and RELT have no such established furin link that I checked." / "I have not pulled the SCZ effect direction for the A allele, so I am not asserting which direction of FURIN dosage is risk-conferring."
- Source (verbatim from RESULT): Susan-Resiga 2011: "In vitro digestion of a 12-mer peptide … showed that furin cleaves the best"; Spanou 2025 title: "Prodomain processing controls BMP-10 bioactivity and targeting to fibrillin-1 in latent conformation."
- Scrutiny: the mechanistic story from two furin papers is offered as "plausibly direct" and immediately bounded by an assay-level caveat; the two unexplained trans hits are declared unexplained.

- **[G13] session=d2370363 msg=7633a679-dfa3-48d3-8f25-6a74db55ff47 time=2026-09-09 20:24:42 user=finucane backend=perplexity**
- Question: (as F12)
- Claim (verbatim): "The behavioural traits … all point the same way as SCZ protection and are plausibly the same signal read through correlated behavioural phenotypes rather than six independent biologies — I have not run colocalization to confirm that."
- Source: PheWAS tool output.
- Scrutiny: pleiotropy is not over-read; the missing analysis is named. (Contrast with F12 in the same message, where the literature side gets no equivalent caveat.)

- **[G14] session=a04b73d4 msg=1473f332-8df7-4a91-ac24-755ee252a9c6 time=2026-07-27 16:58:52 user=finucane backend=perplexity**
- Question: (as F2)
- Claim (verbatim): "[Auer et al. 2014, *Nat Genet*] Exome array, 31,340 individuals: **missense variants in CXCR2 associated with reduced WBC count, gene-based p = 2.6×10⁻¹³**." / "[Akbari et al. 2021, *Science*] 645,626 exomes, BMI: 16 exome-wide significant genes … **GIPR PTVs *and* the missense alleles R190Q/E288G (shown LoF in vitro)**"
- Source (verbatim from RESULT): Auer: "31,340 individuals genotyped on an exome array … gene-based P = 2.6 × 10(-13)"; Akbari: "We sequenced the exomes of 645,626 individuals"
- Scrutiny: sample sizes and p-values transcribed exactly from the abstracts; "in vitro" retained for the functional claim; Rosenkilde 2025 labelled "Review:".

## 4. Patterns

**Backend.** Every literature call in this file is Perplexity; Europe PMC and web_search were never used. Of 99 returned records, 60 resolved to real Europe PMC records and 38 were Perplexity-only snippets (bibliography fragments, figure pages, a supplement PDF, records titled "1" or "42250116"). The assistant handled the snippets well — it never quoted a fragment as a finding and in one case rebuilt a proper citation from a reference-list line (G9). The single persisted Perplexity `summary` was, however, relayed as the paper's own numbers (F2). So the senior user's "Perplexity regurgitates" diagnosis is only a small part of what this file shows.

**The larger source of unscrutinised claims is the model's own knowledge, presented indistinguishably from search results.** Category 7 is the most frequent (9 of 16): mechanisms (C4A, SP4→GRIN2A, FLARE-for-rare-variants), specific numbers (Udler "~50×47", Tansey p-values), locus lore (chr10q24, 17q21.31), drug approvals, and author-less "Nature Genetics 2023" / "Mol Psychiatry 2022" / "Nature Commun 2023" rows all sit in a table headed "Backend queried: perplexity". Several of the medium findings (F8, F10) are memory claims wearing a returned paper's citation. Nothing in the response format marks "from search" versus "from recall".

**The evidence rubric exists for one kind of paper only.** The assistant has a strong, consistently applied rubric for *association* studies — every candidate-gene or small-n association it met was sized, dated and down-weighted (G1, G3, G7), and review/expression/pharmacology papers were labelled by evidence type (G4). It has essentially no rubric for *functional and mechanistic* claims: a HEK293T luciferase assay and a 106-patient glioma/gut-flora study become "the functional literature" (F12); a title-only paper "independently predicts" a result (F9); a mouse paper's introduction becomes a fact about the GWAS (F6); a review "states directly" something not in it (F5). This is exactly the shape of the user's complaint ("demyelination for schizophrenia … based on like 5 people" is a non-association claim). The rubric for loaded data — pseudo-CS vs formal, PIP floor, LD ≠ coloc, `cohort = 'control'`, implausible p-values — is applied every time; the literature side gets it only when the paper is a genetic association.

**Attribution drifts within a message.** The Pass 2 table row is usually more careful than the Pass 3 sentence that reuses it: "notes" → "state directly" (F5); a quoted intro sentence → "call SP4 one of only two genes" (F6); "Zhou 2022 … OR 9.37" → "three independent layers" (F7, double-counting SCHEMA already in the loaded data). The fixed one-cell-per-paper "Key finding" table also invites one-line abstract restatement and puts reviews and primary studies on the same footing.

**Persistence limits the audit.** RESULTs are truncated at ~8 records while `returned` is 10–12, so roughly 15 citations could not be checked at all; category 3 is under-counted here, not over-counted.

**Self-correction is strong but only when triggered by the user or by data.** The two user challenges (peak-gene provenance; FLARE direction) both produced explicit, itemised corrections of the assistant's own earlier statements, and the assistant volunteered a data-QC rejection ("Two MPRA effect sizes are implausible and I will not build on them"). Nothing equivalent happened for a literature claim in these four sessions, and no user message pushed back on one.

**Baseline rate.** 14 counter-examples against 16 findings across 11 literature-bearing messages, with no high-severity finding. The good behaviour is real and frequent; it is concentrated on association-study design and preprint labelling, and thins out as soon as the paper is functional, mechanistic, a review, or recalled rather than retrieved.
