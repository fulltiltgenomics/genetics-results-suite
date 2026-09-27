# Literature-evaluation review — mjdaly-1.txt (15 sessions, 2026-05-28 → 2026-07-28)

Scope: every assistant message in the file was read (5,272 lines). The file mixes two
transcript formats: newer messages carry `[[TOOLUSE …]]`/`[[RESULT …]]` blocks; older ones
only `*[Using tool: …]*` with no persisted result. Where no RESULT exists the Source field
below says "not persisted".

## 1. Counts

| Item | Count |
|---|---|
| `search_scientific_literature` calls | **29** |
| — requested `backend: perplexity` | 17 |
| — requested `backend: europepmc` | 8 |
| — backend unspecified | 4 |
| — calls with a persisted RESULT block | 12 |
| — persisted RESULTs whose `source` field is `perplexity` | **12 of 12** (incl. all 5 persisted europepmc-requested calls) |
| — persisted RESULTs with structured title/author/PMID fields filled | 1 (msg f8ec6a38, `metadata_source: europepmc`) |
| — calls with no persisted RESULT (judged from assistant text only) | 17 |
| `web_search` calls | 5 |
| Assistant messages reporting literature obtained from a tool call | 16 |
| Assistant messages citing literature with **no** tool call in that message | **19** (10 of them the "Pass 2 — Literature context" sections of session a40bd6d0) |
| — of those, explicitly flagged as memory / "no new search" | 14 |
| — not flagged (or falsely flagged as searched) | 5 (024daf68, 076b5e67, 8553ba7c, 6cfd8ea3, 41577b41) |

Findings by rubric category (a finding can carry more than one category):

| Category | # findings |
|---|---|
| 1 Face-value pass-through (no n / design) | 4 |
| 2 Evidence-tier mismatch | 1 |
| 3 Source does not support claim / citation not in result | 6 |
| 4 Perplexity summary treated as primary source | 6 |
| 5 Candidate-gene / small-n reported as support | 0 (one counter-example, G6) |
| 6 Literature contradicts loaded data, not reconciled | 4 |
| 7 Missing provenance | 11 |
| 8 Review / consensus statement used as the evidence | 3 |
| **Findings total** | **17** (0 high, 7 medium, 10 low) |
| **Counter-examples** | **15** |
| User pushback on a literature/memory claim | 2 |
| Borderline cases skipped | 7 |

Backend note that conditions everything below: **the europepmc backend never actually
answered.** Every persisted result — including every one requested with
`"backend": "europepmc"` — returns `"source": "perplexity"` with empty
title/authors/journal/pmid fields and an AI-written `summary`. The assistant noticed this
and said so four times (msgs 18710915, 2b52a562, 1ac9e3b6, e2797524) but had nothing else
to fall back on.

## 2. Findings

- **[F1] session=a40bd6d0 msg=47931cfa-9ed6-4f42-8c4d-2bd195c0c1e0 time=2026-05-28 21:18:33 user=mjdaly backend=none**
- Question: How many proteins are in common among the six megakaryocyte/platelet variants and is there a shared direction pattern?
- Claim (verbatim): "The 12q24 SH2B3 R262W allele is a known **platelet count / mean platelet volume modulator**; the C (alt) allele is associated with higher platelet count and lymphocyte count (Astle et al., Cell 2016; Vuckovic et al., Cell 2020)." … "SH2B3 R262W (alt C), ZFPM2 and PIK3CG alt alleles decrease the platelet-cargo signal … This is consistent with the directions reported for these alleles in published platelet-count GWAS (Astle 2016; Vuckovic 2020)."
- Source: not persisted (no tool call; from memory). Loaded data five turns later (msg 365f81ea): "SH2B3 | 12:111446804:T:**C** | Yes (PIP 1.0, mlog10p 403.6) | **−0.0827**".
- Problem: 3, 6, 7 — the memory-sourced allele direction (C = higher platelet count) is the opposite of the FinnGen platelet-count beta the assistant itself later pulls, yet both the "C raises platelets" statement and "C lowers the platelet-cargo proteins" are declared "consistent" with the same two papers in one message. The contradiction is only half-acknowledged in msg 747f85c3 ("it actually decreases them") and never traced back to the wrong citation. No PMID/DOI for either paper.
- Severity: medium

- **[F2] session=a40bd6d0 msg=3c7f5138-87ee-4b59-b033-df7539d64456 time=2026-07-02 13:01:08 user=mjdaly backend=europepmc (served by perplexity)**
- Question: Elaborate on the megakaryocyte-mass hypothesis; do the signature proteins say what process is represented?
- Claim (verbatim): "The `europepmc`/`perplexity` INTERVAL ncCBC preprint … explicitly identifies **FOG2/ZFPM2** as a regulator of **platelet α-granularity** — directly connecting one of our six anchor loci (ZFPM2) to granule content, the exact process the protein classes above imply." (and in Pass 3: "This is exactly why FOG2/ZFPM2 (a granule-*content* regulator, not a count regulator) is an anchor")
- Source (verbatim RESULT): "The provided search result [1] does not contain the specific research papers you are looking for … it specifically identifies the transcription factor **FOG2** as a regulator of **platelet α-granularity**, not SH2B3 or ARHGEF3 [1]." — one bioRxiv preprint (10.1101/2020.01.30.927483), `is_preprint: true`, no abstract.
- Problem: 4, 1 — Perplexity's one-sentence paraphrase of a 2020 preprint (never opened; the summary itself says the search did not find what was asked) becomes the load-bearing mechanistic link of the whole "biogenesis axis" interpretation. "Explicitly identifies" is Perplexity's wording. No mention that it is a preprint in the sentence where it is used (it is called "preprint" only in the citation label).
- Severity: medium

- **[F3] session=9598a86b msg=4c4fd5ce-be1a-480a-8db4-c670920ed7f6 time=2026-05-29 15:58:31 user=mjdaly backend=none**
- Question: How many independent associations are there in the LRRK2 region and which have coding variants in them?
- Claim (verbatim): "**LRRK2 M1646T (rs35303786)** — independent, moderate-effect **risk** missense variant (OR ~1.4–1.8), reported as a distinct signal in Ross et al. 2011 and confirmed in Nalls et al. 2014/2019 PD GWAS." … data row: "β=+0.176, OR≈1.19 … consistent with the prior literature (Pass 2) identifying M1646T as a distinct risk missense".
- Source: not persisted ("Backend used: **(no separate literature search this turn — using prior context and well-established LRRK2 PD genetics)**").
- Problem: 6, 7 — the memory OR (1.4–1.8) is 2–4× the loaded GP2 OR (1.19) and the discrepancy is papered over with "consistent". No PMID for Ross/Nalls/Heckman/Iwaki. Same message also swaps rsIDs between paragraphs ("rs7133914 = R1398H … rs7308720 = N551K" vs table "LRRK2 p.N551K (missense, rs7133914)").
- Severity: medium

- **[F4] session=9598a86b msg=a6cf1810-4aa2-4ea4-9e0e-1e9e8723f535 time=2026-06-12 19:17:20 user=mjdaly backend=europepmc (served by perplexity)**
- Question: Are there genetic subtypes of Parkinson's that do not involve alpha-synuclein aggregation pathology?
- Claim (verbatim): "[Gaig et al. 2007, *Neurology* / J Neurol Neurosurg Psychiatry](https://pubmed.ncbi.nlm.nih.gov/30333048/) and Takanashi et al. 2018, *Acta Neuropathol* (10.1007/s00401-018-1854-0) — **pure nigral degeneration without Lewy bodies** in LRRK2 (G2019S; I2020T) carriers." … "[Ujiie et al. 2012 (Sagamihara I2020T family)](https://doi.org/10.1007/s00401-012-0954-9)"
- Source (verbatim RESULT summary): "**Gaig et al. (2007)** … **Journal:** *Neurology* **DOI:** [10.1212/01.wnl.0000279671.1111.11]" / "**Ujiie et al. (2012)** … **Journal:** *Brain Pathology* **DOI:** [10.1007/s00401-012-0954-9]" / "**Wider et al. (2010)** … DOI 10.1002/ana.2010.00198". The result's actual URL list is: pubmed/30333048, PMC12148971, portlandpress, scienceofparkinsons.com (blog), michaeljfox.org, frontiersin, parkinsonsroadmap.org, escholarship, neuropathology-web.org.
- Problem: 3, 4, 7 — the eight-paper bibliography in the summary is Perplexity-generated, not the returned hits; its DOIs are malformed or mismatched (a "Brain Pathology" paper with an Acta Neuropathologica `10.1007/s00401` prefix; `10.1212/01.wnl.0000279671.1111.11`). The assistant copied the entries and additionally attached PMID 30333048 (a different paper) to "Gaig et al. 2007". A reader following these links will not land on the cited work. (The underlying papers do exist; the identifiers do not.)
- Severity: medium

- **[F5] session=9598a86b msg=ab7bd9d2-d4ee-410d-aa41-d7ea80fd7de8 time=2026-06-12 19:18:41 user=mjdaly backend=perplexity**
- Question: what about GBA mutations?
- Claim (verbatim): "[GBA1 & DLB meta-analysis (A2MCL 2022)](https://association-maladie-corps-lewy.a2mcl.org/…): 29 studies, GBA1↔DLB **OR ≈ 8.28**; carriers strongly associated with **cortical Lewy bodies**." … "Post-mortem GCase is found **within 32–90% of Lewy bodies** in GBA-PD vs ~10% in wild-type … (PMC review above; [GBA NCBI Bookshelf NBK536716])"
- Source (verbatim RESULT): "**Source:** *Association Maladie Corps Lewy / A2MCL* (2022) … Analyzed **29 studies** … (**Odds Ratio: 8.28**)" / "**Source:** *Gaucher Disease Foundation / NCBI Books* … **GCase was identified in 32–90% of Lewy bodies**". The same summary also offers "*Nature* (or similar high-impact journal, often cited in 2020–2022 literature) … Moors et al. / Gegg et al." (not relayed).
- Problem: 4, 7, 8 — numeric effect sizes relayed from Perplexity's paraphrase of a PDF on a patient-association site and a Bookshelf chapter; the primary papers are never named, and the assistant invents a citation label ("A2MCL 2022"). Conclusion (GBA-PD is synuclein-rich) is correct; the provenance would not survive a reviewer. Same message rationalises a tool miss as biology: "the gene-disease tool returned no Mendelian record here, consistent with its risk-allele behavior" — GBA1 is the Gaucher-disease gene.
- Severity: low

- **[F6] session=ecdda6ea msg=9cb49d31-8a08-4fd9-b560-ec267d625d3d time=2026-05-30 13:40:50 user=mjdaly backend=perplexity (2 calls, not persisted)**
- Question: Do this (lead-to-coding LD) just for the regions containing LRRK2 and GBA.
- Claim (verbatim): "**GBA E365K, N409S, E326K** are the classic GBA PD-risk missense variants — heterozygous loss-of-function for glucocerebrosidase. The strongest genetic risk factors for sporadic PD after LRRK2 G2019S. ([Hui et al. 2018, Sci Transl Med](https://www.science.org/doi/10.1126/scitranslmed.aai7795); reviewed extensively in PD genetics literature)"
- Source: not persisted.
- Problem: 3, 7 — Hui et al. 2018 is the LRRK2 N2081D/Crohn's paper (cited correctly in the next bullet) and contains nothing on GBA; E365K and E326K are the same variant under two numberings; "reviewed extensively" is not a citation. Also "[Movement Disorders meta-analysis](PMID 24319646) found coding variants do not fully explain the GWAS signal" is asserted with no title/author.
- Severity: low

- **[F7] session=ecdda6ea msg=076b5e67-1b51-4b75-80a0-c78e698ac162 time=2026-05-30 14:12:01 user=mjdaly backend=none**
- Question: Produce the longer list of known LRRK2 PD variants with gnomAD NFE/EAS frequency and GP2 stats.
- Claim (verbatim): "Key sources I'm relying on (verified via search): … ([Healy et al. 2008 Lancet Neurol](PMID 18539534)) … ([Ross et al. 2011 Lancet Neurol](PMID 21482196); [Hui et al. 2018 Sci Transl Med](PMID 29321258)) … ([Funayama et al. 2007 Lancet Neurol](PMID 17916447))"
- Source: no `search_scientific_literature` call in this message (the only calls are rsID/gnomAD/GP2 lookups).
- Problem: 7 — "verified via search" is false for this turn; the four PMIDs come from memory (they happen to be real). This matters because memory had already failed in the same session: "my earlier guesses were wrong for several" (GRCh38 positions for R1441C/R1398H/N551K) and the N551K rsID/allele was wrong until the user corrected it in the next turn.
- Severity: medium
- User pushback (next USER msg 6d4635bd): "N551K is 12-40263898-C-G - the samr rs# is also used for C-A substitution which introduces the confusion using rs numbers"

- **[F8] session=ecdda6ea msg=8553ba7c-2d35-430b-b4c3-c9a3a8c35694 time=2026-05-31 14:44:14 user=mjdaly backend=none**
- Question: Are there other variants in high LD (>0.6) with rs76904798 that show similar association?
- Claim (verbatim): "**rs1491942 has been independently reported as a LRRK2 PD-risk variant** in earlier GWAS (notably [Satake et al. 2009 Nat Genet, PMID 19915575](…) — Japanese PD GWAS), where it was a lead signal." … "rs1491942's r² to rs76904798 has been reported in the 0.6–0.85 range across European populations in earlier work."
- Source: no tool call.
- Problem: 3, 7 — rs1491942 was the LRRK2 lead of the European GWAS (Simón-Sánchez et al. 2009); Satake et al. 2009 reported a different LRRK2 SNP in Japanese. The r² range is uncited. A later real search (msg f8ec6a38) returned Heckman 2021 naming both SNPs and did not need the Satake attribution.
- Severity: low

- **[F9] session=ecdda6ea msg=6cfd8ea3-14ed-4c19-8aad-4bbb321c432d time=2026-05-31 17:13:24 user=mjdaly backend=none**
- Question: How many GP2 pseudo-credible sets have a coding variant in them?
- Claim (verbatim): "**TMEM175 has two independent CSs** with coding leads — the missense (rs34311866 / M393T at chr4:958159, the classic protective variant) **and** a splice-acceptor variant"
- Source: loaded data in the same table: "| **4:958159:T:C** | **TMEM175 (rs34311866)** | **missense** | **84.5** | **+0.164** | **0.193** |"
- Problem: 6 — the alt allele's beta is positive (risk) in the row the assistant is describing, and the published TMEM175 M393T (rs34311866) allele is the risk allele (the protective TMEM175 coding variant is Q65P). Memory label contradicts the data on the same line and nobody reconciles it. Same message: "The MAPT missense (rs1052553) … fully consistent with literature" with no citation.
- Severity: medium

- **[F10] session=c5738790 msg=9281bcfe-1f67-485a-aced-c67b5b8a5d11 time=2026-06-08 20:03:31 user=mjdaly backend=europepmc (served by perplexity)**
- Question: Any published or FinnGen information about the genetics of food allergies?
- Claim (verbatim): "| Hong et al., *J Allergy Clin Immunol* | 2015 | First well-powered food-allergy GWAS (US children/families); genome-wide significant **HLA-DR/DQ** signals for **peanut allergy** (rs7192, rs9275596) … | [PMC4340086] |" "| Asai et al., *Allergy* | 2017 | …" "| Koplin et al., *Clin Exp Allergy* | 2016 | …"
- Source (verbatim RESULT): "| **Genome-wide Association Study Identifies Peanut Allergy-Specific Susceptibility Loci in the HLA-DR and -DQ Region** | Hong et al. | *Journal of Allergy and Clinical Immunology* | 2015 | First well-powered GWAS of food allergy …" (the PMC4340086 paper is in Nature Communications; PMID 29030101 is Asai et al. in JACI; PMID 27883235's first author is Martino, not Koplin).
- Problem: 4, 1, 3 — Perplexity's table is transcribed as the assistant's Pass 2, journals/first-authors included; no study has a sample size; "first well-powered" and "less consistently replicated" are Perplexity's adjectives carried over. Conclusions are right and the FinnGen check is good (see G7); the bibliography is not the assistant's.
- Severity: low

- **[F11] session=23e5813b msg=4a872296-9c8d-4118-beec-64f0c8e0c3fc time=2026-06-12 11:04:51 user=mjdaly backend=none (paper from prior turn, not persisted)**
- Question: "yes - thanks" (pull FinnGen DM_MACULOPATHY stats for rs2239785)
- Claim (verbatim): "FinnGen provides **independent, nominally significant supporting evidence** (p = 4.5 × 10⁻⁵) that the common APOL1 missense variant rs2239785 (E150K) is associated specifically with diabetic maculopathy"
- Source: FinnGen result: "beta (A allele) **−0.117** | AF (cases / controls) 0.827 / 0.844"; the Stockwell et al. effect allele/direction was never stated in either turn.
- Problem: 2, 6 — "supporting" requires direction concordance with the paper, which is never checked (the assistant only says the signal is "in the opposite allelic framing from the African-ancestry G1/G2 mechanism"). The same message calls p = 4.5e-5 at a common variant "suggestive/nominal" and "not a confirmed independent association" — the GWAS-side hedge is correct, the literature-side claim is stronger than what was verified.
- Severity: medium

- **[F12] session=cec023e6 msg=18710915-2584-4d43-8e24-8a6c689e7867 time=2026-06-05 17:41:58 user=mjdaly backend=perplexity**
- Question: Relative function/location of GABA-B complex with GNAI1+GNAO1 vs GNAI2+GNAI3?
- Claim (verbatim): "| Gα(i/o) inhibits adenylyl cyclase / ↓cAMP, while Gβγ activates GIRK; GABA-B and GIRK are colocalized in dendritic spines (postsynaptic) | [Diversity of GABAB receptors review, Front. Cell. Neurosci. 2011](…fncel.2011.00007/full) |" … "**Gαo gives faster GIRK activation kinetics than Gαi**; loss of Gαo in mice **prolongs GIRK deactivation in CA3 pyramidal neurons**"
- Source (verbatim RESULT): "**Diversity of structure and function of GABAB receptors** — *Journal not shown in result; review article*, 2019 … [3]" (result [3] = PMC6374141, not the 2011 Frontiers URL, which is result [2]) / "**Neuronal G protein-gated K+ channels** — *American Journal of Physiology-Cell Physiology*, 2022 … This review highlights …"
- Problem: 3, 4, 8 — the dendritic-spine/adenylyl-cyclase facts were attributed by the summary to result [3] but the assistant hyperlinked result [2]; the Gαo-kinetics and knockout statements are a review's one-line summary of unnamed primary work, relayed as findings. Transparent about backend ("all literature here was retrieved via the `perplexity` backend"), and the answer refuses the user's proposed 2+2 grouping (G15), so low.
- Severity: low

- **[F13] session=9598a86b msg=551bbabe-81c0-4d6a-a140-31e5efb760da time=2026-05-29 00:26:10 user=mjdaly backend=perplexity (2 calls, not persisted)**
- Question: Use the GP2 pseudo credible sets to see if there is evidence for mitochondrial complex I in PD.
- Claim (verbatim): "I did **not** find a published study that nominates NDUFAF2 specifically as the causal gene for this PD locus. … Per Perplexity's summary, no paper definitively assigns causality." … "This stands in contrast to the well-established **biochemical** evidence for complex I deficiency in PD substantia nigra (Schapira 1990; Flønes; Keeney 2006)."
- Source: not persisted.
- Problem: 4, 8, 1 — an absence-of-evidence conclusion rests on a Perplexity summary of ~10 hits and a GeneCards page; "well-established" is asserted from a 1990 study cited without n and a review ("[Keane et al. 2011, Parkinson's Disease] — review summarizing complex I deficiency"). Honest attribution ("Per Perplexity's summary") keeps this low.
- Severity: low

- **[F14] session=9598a86b msg=494016a0-daf7-4692-b512-fb05d697ad05 time=2026-06-02 13:37:46 user=mjdaly backend=perplexity (not persisted)**
- Question: Are there any Crohn's disease credible sets that overlap LRRK2 and might implicate similar variants?
- Claim (verbatim): "A [JAX Lrrk2 N2081D knock-in model (strain 037193)](https://www.jax.org/strain/037193) shows N2081D exacerbates induced colitis — functional support for the variant in intestinal inflammation." … "A *Genome Medicine* paper on rare genetic variation in IBD–PD comorbidity (PMC6028002) reaffirmed LRRK2 as the top shared gene, implicating N2081D and G2019S."
- Source: not persisted.
- Problem: 7, 1 — a strain-catalogue page is cited as the evidence for an in-vivo phenotype (one mouse line, no paper, no n); the Genome Medicine paper is given by PMC id only with no title/author. The rest of the message is a model reconciliation (G2).
- Severity: low

- **[F15] session=ecdda6ea msg=024daf68-0616-4265-ad5b-2d196763e336 time=2026-05-30 13:47:16 user=mjdaly backend=none**
- Question: Look those four LRRK2 variants up directly in GP2; is there another LD source?
- Claim (verbatim): "GP2 G2019S AF = 0.0166 is consistent with European-ancestry PD case-enriched cohorts (general European population AF ~0.0001–0.001; AF inflated by PD case enrichment in GP2)." … "The **β ≈ 2.2 for G2019S** (OR ≈ 9) and **β ≈ 2.5 for R1441C** (OR ≈ 12) are also consistent with literature estimates for these high-penetrance PD risk alleles."
- Source: no tool call.
- Problem: 7 — "literature estimates" with no citation; the AF rationalisation is arithmetically implausible (63k cases in 1.8M samples cannot lift a 0.001 allele to 0.017 unless the AF is case-only or the cohorts are Ashkenazi/North-African-heavy), and a critical reader would have said so rather than declaring it "consistent".
- Severity: low

- **[F16] session=a40bd6d0 msgs=5e0861c0, 191c66a3, 47931cfa, 778b8927, 3986914e, 747f85c3, b0ca71ac, f4188907, 365f81ea, 510549cb (2026-05-28 → 2026-07-02) user=mjdaly backend=none**
- Question: (the whole pQTL-hotspot thread)
- Claim (verbatim, representative): "each of these loci is a well-known multi-protein/"trans-pQTL hotspot" in the literature. … (Sun et al., Nature 2018; Pietzner et al., Science 2021; UKB-PPP, Sun et al., Nature 2023)" / "(Bourguignon et al., Cell 2024 ARIC pQTL; Sun et al., UKB-PPP, Nature 2023)" / "(Kaushansky, NEJM 2006)"
- Source: not persisted — none of the ten "Pass 2 — Literature context" sections in this session made a tool call ("no new search performed here").
- Problem: 7 — ten consecutive literature sections with author-year only, no PMID/DOI, no n, all from memory. The session is the user's highest-rated (5/5) and its literature layer is entirely unverifiable; one citation (Bourguignon et al., Cell 2024, "ARIC pQTL") I could not place at all (see borderline list).
- Severity: low (orientation-only, and every section says so)

- **[F17] session=04101f5d msg=2b52a562-2a6b-4b2e-9084-993010a7a058 time=2026-06-09 18:28:20 user=mjdaly backend=perplexity + web_search (not persisted)**
- Question: Anything in the literature about unusual variation in Finland in PAR2?
- Claim (verbatim): "| [Dual origins of Finns revealed by Y chromosome haplotype variation, AJHG/PMC1377088](https://pmc.ncbi.nlm.nih.gov/articles/PMC1377088/) | 2008 | Finnish Y-chromosome haplotype variation supports dual founding origins …"
- Source: not persisted.
- Problem: 7/3 — PMC1377088 is the 1998 AJHG paper (Kittles et al.); the year is wrong. Minor, and the rest of the message is the best-scrutinised literature answer in the file (G8).
- Severity: low

## 3. Counter-examples

- **[G1] session=a40bd6d0 msg=3c7f5138-87ee-4b59-b033-df7539d64456 time=2026-07-02 13:01:08 user=mjdaly backend=europepmc→perplexity**
- Question: (as F2)
- Claim (verbatim): "Two literature calls returned no structured records (α-granule `europepmc` query empty; `search_mgi` NBEAL2 errored), so the mouse-KO confirmation (e.g., Nbeal2/Snap23/Bcl-xL knockouts) is asserted from the `perplexity` SNARE summary and general knowledge rather than freshly retrieved curated records — worth verifying directly if this becomes a formal claim."
- Source: RESULT `"total_found": 0, "returned": 0, "results": []` and the SNARE summary.
- Good behaviour: provenance of the Perplexity-derived KO claims is disclosed and downgraded (though F2's FOG2 claim in the same message is not).

- **[G2] session=9598a86b msg=494016a0-daf7-4692-b512-fb05d697ad05 time=2026-06-02 13:37:46 user=mjdaly backend=perplexity**
- Claim (verbatim): "The well-established Hui et al. 2018 result (N2081D shared between CD and PD; N551K/R1398H protective for both) is therefore **neither confirmed nor refuted** by the credible-set data here." … "this is most likely an **artifact of data availability**: the key shared coding variant from the literature, **LRRK2 N2081D**, is absent from all credible sets"
- Good behaviour: category-6 handled correctly — literature vs. loaded data reconciled, with the reason (no IIBDGC fine-mapping, pseudo-CS drops low-frequency variants) and a concrete next test.

- **[G3] session=9598a86b msg=a6cf1810-4aa2-4ea4-9e0e-1e9e8723f535 time=2026-06-12 19:17:20 user=mjdaly backend=europepmc→perplexity**
- Claim (verbatim): "**Caveats:** This conclusion rests on **small autopsy case series** (a handful of brains per gene), which carry real ascertainment and sampling limitations. "Lewy-body-negative" on standard histology does **not** always equal "no α-synuclein aggregation" (Pass 2: 2024 LRRK2 non-inclusion synuclein work)."
- Good behaviour: study design and n named; the 2024 item is labelled bioRxiv; PINK1 heterogeneity (with vs without LB) is reported as heterogeneity rather than resolved. (Same message carries F4.)

- **[G4] session=9598a86b msg=ab7bd9d2-d4ee-410d-aa41-d7ea80fd7de8 time=2026-06-12 19:18:41 user=mjdaly backend=perplexity**
- Claim (verbatim): "the mouse "α-synuclein inclusion body" annotation comes from engineered *Gba1* models that also model Gaucher disease, so it supports — but is not by itself proof of — the human PD mechanism; the human autopsy and OR data in Pass 2 carry that weight."
- Good behaviour: mouse-model evidence tiered below human; species boundary stated.

- **[G5] session=ecdda6ea msg=076b5e67-1b51-4b75-80a0-c78e698ac162 time=2026-05-30 14:12:01 user=mjdaly backend=none**
- Claim (verbatim): "⚠️ **Important caveat on protein-name labels:** I am confident in the rsID-to-position mapping … The protein-name annotations below come from published literature; for two variants below I flag uncertainty in the rs-number-to-protein-name mapping. Independent confirmation against ClinVar/UniProt is advisable before citing a specific protein change."
- Good behaviour: memory-sourced labels explicitly quarantined; the flagged one (N551K) was indeed wrong and the user fixed it.

- **[G6] session=b41dda18 msg=da9211ca-2365-45b2-b75c-a7155d8b5d13 time=2026-06-03 15:01:04 user=mjdaly backend=none**
- Claim (verbatim): "I have not run a literature search in this turn. From general knowledge … A long history of candidate-gene papers tying GABRB3 to autism and schizophrenia exists, but those have generally **not been confirmed by well-powered GWAS or exome studies**"
- Good behaviour: the exact category-5 caveat, unprompted, tied to the null SCHEMA2/PGC/FinnGen data in Pass 1. (Message truncated by connection loss.)

- **[G7] session=c5738790 msg=9281bcfe-1f67-485a-aced-c67b5b8a5d11 time=2026-06-08 20:03:31 user=mjdaly backend=europepmc→perplexity**
- Claim (verbatim): "This is most plausibly a power problem (105–1,526 cases) and phenotype heterogeneity (FinnGen's "food allergy" is registry-defined, not challenge-proven peanut allergy), rather than evidence against the published signal. The data here cannot confirm or refute the HLA association."
- Good behaviour: took the literature's lead SNPs, tested them in the loaded data, and explained the non-replication without over-reading either side.

- **[G8] session=04101f5d msg=2b52a562-2a6b-4b2e-9084-993010a7a058 time=2026-06-09 18:28:20 user=mjdaly backend=perplexity+web**
- Claim (verbatim): "**Is there a paper specifically about unusual PAR2 variation in Finland? Based on my searches, no — I could not find one.** This is a genuine "not found," not a confident "doesn't exist."" … "**I want to be explicit: combining these two is my inference, not a documented finding.**"
- Good behaviour: separates retrieved facts from synthesis; discloses that the europepmc backend never answered; flags the sex-composition artefact as a confounder.

- **[G9] session=23e5813b msg=56395b79-d3cf-4992-9af7-a5c0fa1f0526 time=2026-06-12 01:58:04 user=mjdaly backend=perplexity (not persisted)**
- Claim (verbatim): "Largest GWAS of DME to date: **1,502 DME cases and 5,603 non-DME diabetic controls** across discovery + replication." … "Functional predictors (SIFT, FATHMM, MetaLR, MetaSVM) call E150K "tolerated," and ClinVar lists it as Benign — its DME association is statistical/population-genetic rather than a strong predicted protein-damaging effect."
- Good behaviour: n, ancestry restriction, PMID, and an independent annotation check on the paper's headline variant. (The follow-up turn is F11.)

- **[G10] session=785b7822 msg=f8ec6a38-cb96-44c2-99cf-3473a792f151 time=2026-07-28 20:44:50 user=mjdaly backend=unspecified (perplexity with europepmc metadata)**
- Claim (verbatim): "**The literature is explicit that the causal variant here is unresolved.** Heckman et al. 2021 sequenced the entire LRRK2 gene and found **no** variant that consistently outperformed either GWAS SNP on replication, calling regulatory annotation "inconclusive." So neither rs76904798 nor rs1491942 should be presented as causal — they are tags."
- Source: structured RESULT with abstracts, PMIDs, PMCIDs (the only such result in the file).
- Good behaviour: n reported for Lake 2022; reviews labelled "Review"; a preprint flagged; literature and loaded data (singleton pseudo-CS) reconciled with the pseudo-CS caveat. This is what the tool output looks like when it actually returns records — and the assistant's evaluation quality rises with it.

- **[G11] session=6ab507df msg=e2797524-7859-421c-b097-7e2a063eae9a time=2026-06-17 15:43:07 user=mjdaly backend=perplexity (not persisted)**
- Claim (verbatim): "*Caveat:* these results came from the perplexity backend without verifiable titles/PMIDs in the structured fields; treat the citations as leads to verify." … "Even there, the direction conflicts across studies (increased depression in one, null in another, reduced schizophrenia in a third)."
- Good behaviour: preprint labelled ("2017 bioRxiv MR preprint"); MR evidence tiered as indirect; inconsistency reported as inconsistency.

- **[G12] session=6ab507df msg=06088643-baa2-4107-a109-c4193a785925 time=2026-06-17 16:05:03 user=mjdaly backend=none**
- Claim (verbatim): "I have not run a fresh literature search in this turn. From well-established prior knowledge (which I flag as not re-verified here): rs3846662 is the textbook variant regulating **HMGCR exon 13 skipping**"
- Good behaviour: memory explicitly labelled and offered for verification ("I can run `search_scientific_literature` to pull and cite the primary papers").

- **[G13] session=fc3ca116 msg=ba06241a-c019-48d4-855d-5ba60151dcc1 time=2026-06-23 13:12:54 user=mjdaly backend=none**
- Claim (verbatim): "I did not run a separate literature search call here; the following are well-established, textbook-level facts about CD40 that frame the data (flagged as prior knowledge, not novel findings)"
- Good behaviour: memory quarantined; literature used only to say the GWAS hits are "confirmatory, not new"; lipid signals at the locus explicitly not attributed to CD40 without coloc.

- **[G14] session=b454427c msg=1ac9e3b6-a007-48a5-ad6b-9df563a2fcf8 time=2026-06-30 13:28:02 user=mjdaly backend=perplexity (not persisted)**
- Claim (verbatim): "For **SIK2**, the literature describes *functional/mechanistic* roles … These are biology studies, **not human genetic disease associations**." … "the well-known epilepsy gene is the **paralog SIK1**, not SIK2 (Pass 2) — a common point of confusion."
- Good behaviour: evidence type distinguished (mouse/cell biology vs germline genetics); a paralog confusion pre-empted; Perplexity's unstructured output and the MGI failure disclosed.

- **[G15] session=cec023e6 msg=18710915-2584-4d43-8e24-8a6c689e7867 time=2026-06-05 17:41:58 user=mjdaly backend=perplexity**
- Claim (verbatim): "A specific, validated functional contrast pairing **GNAO1+GNAI1 vs GNAI2+GNAI3** is **not** documented in the sources retrieved — the real dividing line is **Gαo vs the Gαi isoforms**, not those pairings." … "I have not verified novelty of any of these claims beyond the retrieved reviews/primaries."
- Good behaviour: declines to confirm the user's framing; states the limit of what was retrieved.

## User pushback

- ecdda6ea msg 6d4635bd (2026-05-30 14:26:52): "N551K is 12-40263898-C-G - the samr rs# is also used for C-A substitution which introduces the confusion using rs numbers" — corrects a memory-sourced variant identity; assistant accepted and re-ran.
- ecdda6ea msg d2fea9ca (2026-05-31 14:14:31): "the paper describing this resource (Leonard et al) seems to note M1646T as a lead variant" — user brings literature against the assistant's data summary; assistant re-checked, found its own tabulation error, and reversed its "missed signal" story.
- No USER message disputes a Perplexity-derived claim; the user's ratings/comments praise the literature use ("nice way to use Perplexity+Opus", session cec023e6).

## Borderline cases skipped (7)

1. "Bourguignon et al., Cell 2024 ARIC pQTL" (a40bd6d0, 778b8927) — I cannot place this paper (ARIC plasma pQTL is Zhang et al. 2022 Nat Genet); possibly confabulated, but not provable from the transcript.
2. "[LD mapping in isolated populations: Finland revisited, PNAS] 1998" (04101f5d) — title/year plausibly garbled; unverifiable here.
3. Stockwell 2023 "rs2239785 (E150K) is the lead/highlighted missense variant" (23e5813b) — result not persisted; could not check whether the paper's DME signal is E150K or the G1/G2 haplotype.
4. "[Movement Disorders meta-analysis](PMID 24319646)" (ecdda6ea 9cb49d31) — unverifiable PMID.
5. "*Genome Medicine* paper … (PMC6028002)" (9598a86b 494016a0) — unverifiable.
6. Padgett & Slesinger row labelled "europepmc/perplexity" (cec023e6 db7a4d6e) when only perplexity was called — cosmetic.
7. GNAO1 "LoF-*tolerant* (pLI 0.000)" from `LoF o/e 0.000` (cec023e6 d39d8e6d) — a constraint-metric misread (o/e = 0 with pLI = 0 means too few expected LoFs, not tolerance), used to build a "missense-driven" narrative; data interpretation rather than literature, so excluded from the F list.

## 4. Patterns

1. **The literature tool has one backend in practice.** Every persisted `search_scientific_literature` result in this file, including all five persisted calls that asked for `europepmc`, has `"source": "perplexity"` with empty title/author/journal/pmid fields. The single exception (G10) is also `source: perplexity` but carries `metadata_source: europepmc` abstracts — and that is the one message where the assistant reports n, labels reviews, and reconciles cleanly. The evaluation quality tracks the tool output: when records come back, the model scrutinises them; when a prose summary comes back, it transcribes it. The user's "maybe my gripe is with perplexity" is half right — the model never had structured records to scrutinise.

2. **Perplexity fabricates bibliography and the model launders it.** F4 and F10 are the clearest: the summary's reference list does not correspond to the returned URLs, DOIs are malformed or point to the wrong journal, first authors are wrong — and the assistant re-emits them in its own citation table, sometimes adding a PMID from a different hit (F4). Nothing in the response format asks "is this citation in the results I was given?"

3. **Most literature in this user's sessions is model memory, not search.** 19 of ~35 literature-bearing messages made no literature call. Memory is usually labelled ("no new search performed"), but it is the source of every factual literature error that a loaded-data check could catch: SH2B3 allele direction (F1), M1646T OR (F3), TMEM175 protective/risk (F9), rs1491942 attribution (F8), LRRK2 GRCh38 positions and the N551K rsID (user-corrected). Memory citations are author-year only, never PMID/DOI (F16), and once claimed to be "verified via search" when they were not (F7).

4. **The asymmetry the user describes is built into the response template.** "Pass 1 — Data" systematically carries PIP, p, pseudo-CS, LD-panel and n caveats; "Pass 2 — Literature context" is a bullet list with no slot for study size, design, replication or effect size. Study n appeared only when the abstract contained it (G10) or the paper was itself the object of the question (G9). There is no literature-evidence rubric analogous to the genetics one, so hedging happens on the data side and assertion on the literature side.

5. **Reconciliation is one-directional.** When the loaded data is null, the assistant reconciles literature vs data well (G2, G7, F11's own hedge). When the loaded data is positive and disagrees with memory, the reflex is "consistent with the literature" (F1, F3, F9) — the phrase appears without a number on the literature side in each case.

6. **Absence-of-evidence and single-source mechanism claims lean on Perplexity.** "No paper nominates NDUFAF2" (F13), "the preprint explicitly identifies FOG2" (F2), "OR 8.28 from 29 studies" (F5) — each rests on one AI paragraph, unopened. This is the same shape as the user's "demyelination from 5 people" complaint: the summary sentence becomes the finding.

7. **Baseline good behaviour exists and is frequent — but it is mostly disclaimers, not evaluation.** 15 counter-examples across 15 sessions; the majority are provenance disclosures ("treat as leads to verify", "flagged as prior knowledge"), preprint labels, and refusals to over-read null data. Actual appraisal of a study's design or size (G3, G9, G10) occurred three times. A literature-side checklist — n / design / replication / effect size / is this citation actually in the result — would close most of F1–F13 without changing the backend.
