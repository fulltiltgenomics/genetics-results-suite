# Literature-evaluation review: mjdaly-2.txt (15 sessions, 2026-07-01 to 2026-07-24)

Every assistant message in the file was read. Eight sessions contain no literature at all
(9p21 QTLs, zymogen liver fraction, three UKBB pQTL-pleiotropy sessions, ATP2B2, World Cup,
Parkinson's dataset list); the findings below come from the other seven plus a few
memory-only literature passages.

## 1. Counts

| Item | Count |
|---|---|
| `search_scientific_literature` calls | **13** |
| — requested `backend=perplexity` | 7 |
| — requested `backend=europepmc` | 6 |
| — RESULT persisted | 8 — **all 8 have `"source": "perplexity"`** with empty title/authors/journal/pmid fields, including all 5 persisted `europepmc` requests |
| — RESULT not persisted (older format) | 5 |
| `web_search` calls | 3 (1 persisted: duckduckgo, Kanai et al. medRxiv; 2 not persisted: proteinatlas.org) |
| Primary papers actually opened (WebFetch/PMC read) | **0** |
| Assistant messages reporting literature from a tool result | 11 |
| Assistant messages citing literature/"well-known" facts with **no** tool call | 6 (9f71c66f 9p21/ANRIL; c485d738 GCKR/SH2B3/SERPINA1/ASGR1 allele names; 35b34a43 and c14ad0e0 "well-known hub pQTL loci"; 47d348ea "established regulators of platelet counts"; **076f1537 GBA — a full "Pass 2 — Literature context" with zero citations**) plus 2 memory-and-search blends (34ea6dca pancreatitis, 9019b359 pQTL hotspots) |
| Findings (F) | 12 |
| Counter-examples (G) | 8 |
| User pushback on a literature claim | 1 (session 1b236387, msg b3eff4e5) |
| Borderline cases skipped | 5 (see end) |

Findings by rubric category (a finding can carry several):

| Cat | 1 face-value | 2 tier mismatch | 3 source≠claim | 4 Perplexity as primary | 5 candidate-gene | 6 lit vs data unreconciled | 7 provenance | 8 review as evidence |
|---|---|---|---|---|---|---|---|---|
| n | 3 | 2 | 4 | 6 | 1 | 4 | 5 | 2 |

Severity: high 2 (F2, F4), medium 5 (F1, F5, F7, F8, F12), low 5 (F3, F6, F9, F10, F11).

## 2. Findings

- **[F1] session=3cc21a7a msg=ac8e15df-59eb-4763-b853-d27a4d4be3a4 time=2026-07-09 03:05:39 user=mjdaly backend=perplexity (europepmc requested)**
- Question: "are there any phenotypic associations implicating GATA2?"
- Claim (verbatim): "This large VV GWAS (135,514 cases / 675,111 controls) reports a **genome-wide-significant signal at 3q21.3 intronic near GATA2**, and links the locus to immune-cell dysregulation (mast cells, monocytes, macrophages) in varicose veins. This directly corroborates the FinnGen varicose-veins signal" / Pass 3: "This is **externally replicated** by the 810k-individual VV GWAS"
- Source (verbatim): "VVs show **increased mast cells, monocytes, and macrophages** compared to normal veins [1]" ... "Published as a **preprint on bioRxiv** (2020.05.14.095653v1) [1]" ... "**Authors**: Not listed in the snippet"
- Problem: 4, 1, 8 — the mast-cell/monocyte sentence is Perplexity's paraphrase of a histology statement in the preprint's background, relayed as if the paper "links the locus" to it. "Externally replicated" is asserted although (a) the source is a 2020 preprint the assistant never checked for a peer-reviewed version, and (b) the FinnGen row being "replicated" is the `finngen_ukbb` pseudo-CS, and the 810k VV GWAS is UKB-based — overlapping samples are not external replication. The assistant did label it bioRxiv 2020 (see G1) but drew no consequence from that.
- Severity: medium

- **[F2] session=3cc21a7a msg=cf6c44a8-3817-449d-938c-92f365a8407a time=2026-07-09 12:27:52 user=mjdaly backend=perplexity (europepmc requested)**
- Question: "are there associations that implicate IRF7 in common disease phenotypes"
- Claim (verbatim): "**IRF7 rs1131665 (Q412R), a gain-of-function missense variant → Systemic Lupus Erythematosus (SLE)**, replicated across European-American, Asian, and African-American populations." / "This is a well-replicated, mechanistically-anchored association" / "the published literature does implicate IRF7 in a common disease"
- Source (verbatim): "Genetic variants in the **IRF7** gene, particularly the common variant **rs1131665**, are robustly associated with **Systemic Lupus Erythematosus (SLE)** across multiple ethnic populations, conferring risk through **gain-of-function** mechanisms [1][2][3]." Cited [2] "Interferon Regulatory Factors in Human Lupus Pathogenesis", [3] "Interferon regulatory factor signaling in autoimmune disease" (reviews)
- Problem: 2, 4, 5, 8 — the same message hedges the loaded data meticulously ("PIP 0.024 in a 56-variant credible set", "pseudo credible sets ... interpret PIPs with extra caution") but grades the literature claim "well-replicated" on the strength of Perplexity's adjective "robustly". No sample size, OR, or study type is given; the rs1131665 finding is a candidate-gene follow-up of the PHRF1/IRF7 GWAS haplotype (which the message itself notes is "LD-smearing"), and three of the four links are reviews. The reconciliation offered ("largely invisible in FinnGen/UKB ... SLE is a low-prevalence endpoint with limited power") is speculation, not a test — rs1131665 was never queried, though the assistant offered to.
- Severity: high (the answer's headline is that the "genuine" IRF7 disease association lives in the literature)

- **[F3] session=3cc21a7a msg=7b1ad195-54fa-4113-b6b5-ab6eb8ded728 time=2026-07-09 12:48:03 user=mjdaly backend=perplexity (europepmc requested)**
- Question: "Do any of these 11 genes have phenotypic associations ... where coding variants are in the credible set?"
- Claim (verbatim): "GWAS of white-blood-cell counts confirm the CEBPE locus (chr14q11) is associated with **basophil count** and granulocyte differentiation, and CEBPE coding variants are linked to acute lymphoblastic leukemia risk ([PMC5968624], [PMC5005433], [PMC8546033]). This corroborates the PIP=1.0 basophil signal"
- Source (verbatim): "**Population** | Nearly 12,000 US Hispanics/Latinos [1]" ... "the study does mention that *other* **CEBPE variants** (including missense or amino acid substitutions) have been associated with **leukemia risk** and are predicted to be damaging [1]" ... "[2] ... did not specifically highlight a *CEBPE* missense variant for basophils" ... "[3] In a UK Biobank GWAS, **basophils** showed a significant genetic correlation with childhood ALL"
- Problem: 3, 4 — three links are attached as a block to a composite sentence, but only [1] concerns CEBPE-basophil, [2] explicitly did not, and [3] is a genetic-correlation result. "CEBPE coding variants are linked to ALL" is a second-hand aside in the summary ("the study does mention"), relayed as fact. The assistant did note the regulatory-vs-missense discrepancy (G3), so the damage is limited.
- Severity: low

- **[F4] session=1b236387 msg=6b91ac87-5d08-45f8-ae4a-745914625acc time=2026-07-11 18:22:15 user=mjdaly backend=perplexity**
- Question: "is it understood how SERPINA6 impacts serum/plasma cortisol levels"
- Claim (verbatim): "**Yes — the mechanism is reasonably well understood.**" / "[Crawford et al., J Hum Genet 2020] — eQTL/GWAS colocalization shows the causal gene is **SERPINA6 (liver expression)**, not SERPINA1; higher-expression allele → higher plasma cortisol" / "the literature (Crawford 2020/2023) confirms colocalization of the eQTL and cortisol-GWAS signals on SERPINA6"
- Source: not persisted
- Problem: 6, 2, 1 — "confirms colocalization" is stated with no coloc statistic, sample size or tissue, and with more confidence than the loaded data allows: the assistant's own table shows the cortisol lead (PIP 0.17, intergenic) and the pQTL leads as separate credible sets but it did not check LD/coloc before endorsing the literature's mechanism. The next user turn did the check, and the assistant retracted ("my Pass-3 statement in the previous turn ... is **not supported** by the colocalization/LD data. You correctly spotted this."). **User pushback: yes** (msg b3eff4e5: "It seems from the above summary that the associations to cortisol levels and the associations to CBG protein levels are not colocalizing").
- Severity: high

- **[F5] session=1b236387 msg=15853e89-41e6-47c2-b1af-9323c6b4e0cd time=2026-07-12 14:38:44 user=mjdaly backend=perplexity (europepmc requested)**
- Question: "check specifically to see if there are any eQTLs in the liver ... and whether they colocalize"
- Claim (verbatim): "The one external liver SERPINA6 eQTL that has been reported (Cell Genomics 2024, STARNET) explicitly **fails to colocalize** with the cortisol GWAS." / "**colocalization between the cortisol GWAS and SERPINA6 liver expression found NO shared causal variant** (reported posterior for H4 ≈ 1.45×10⁻¹⁵)"
- Source (verbatim): "**no shared causal variant was detected** (posterior probability = 1.45×10⁻¹⁵)" ... "**Authors**: Likely includes researchers from the STARNET consortium" ... "**DOI**: Likely 10.1016/j.xgen.2023.100347"
- Problem: 4, 3, 6 — a "posterior probability" of 1.45e-15 is not a plausible coloc PP.H4 (it reads like a p-value the summary mislabelled); the summary hedges its own title, authors and DOI with "Likely"; yet the bottom line says "explicitly fails to colocalize". The Pass 2 caveat (G5) is dropped from Pass 3 and the bottom line. Two literature claims in opposite directions — Crawford "confirms colocalization" (F4) and Cell Genomics "no shared causal variant" — are never reconciled; the assistant simply switched to whichever agreed with the loaded data. "Crawford et al. (2021) ... 25,314 individuals" is relayed as a third citation while the prior turn called the same paper "J Hum Genet 2020".
- Severity: medium

- **[F6] session=9d6ddfb4 msg=34ea6dca-a5e0-4440-bca9-adf1526bebc3 time=2026-07-12 19:27:41 user=mjdaly backend=perplexity**
- Question: "assess whether the FinnGen acute pancreatitis GWAS ... is consistent with existing literature"
- Claim (verbatim): "**CTRC** (chymotrypsin C): loss-of-function impairs trypsin degradation → increased active trypsin; established ~5–10× risk factor. [[PubMed 37389024]]" / "**TRPV6** — Ca²⁺ channel; TRPV6 variants are an established pancreatitis gene"
- Source: not persisted
- Problem: 7, 1 — "~5–10×" is presented as established with one unverified PMID and no design (published CTRC ORs are for chronic pancreatitis, per-variant, and heterogeneous); TRPV6 is "established" with no citation at all, and the literature evidence (rare LoF in early-onset chronic pancreatitis) is a different tier from the loaded evidence (a common synonymous variant, PIP 0.99, for acute pancreatitis, ~100 kb from the PRSS1/PRSS2 signal) — the tier difference is not mentioned. Witt et al. 2006 and Rosendahl et al. 2018 are author-year citations with no link, indistinguishable from search output.
- Severity: low (conclusion is right; the confidence tiering is absent)

- **[F7] session=8fcc1cb4 msg=47a7d09e-fbb9-4cd2-9d66-c10063904b1e time=2026-07-13 18:55:53 user=mjdaly backend=perplexity (europepmc requested)**
- Question: "Does this variant [chr2:9521321:A:G, ADAM17] colocalize with any molecular QTLs ... what process might be implicated?"
- Claim (verbatim): "ADAM17 also sheds **EGFR ligands (e.g., TGF-α)** essential for intestinal epithelial repair. [Chalaris et al. 2010, JCI](https://pmc.ncbi.nlm.nih.gov/articles/PMC2916135/)" / "murine ADAM17 hypomorphs show **increased susceptibility to DSS colitis** ... [Brandl et al. 2010](https://pmc.ncbi.nlm.nih.gov/articles/PMC4816818/)" / "[Blaydon et al. 2011, NEJM]"
- Source (verbatim): result[0] = PMC4816818 is cited as "[1] **Epithelial Cell-Derived ADAM17 Confers Resistance...** | Chalaris et al., *Journal of Clinical Investigation* | 2010"; result[4] = PMC2916135 is "[5] **Critical role of the disintegrin metalloprotease ADAM17...** | Brandl et al., *European Journal of Immunology* | 2010"; "ovhangir et al., *New England Journal of Medicine* | 2011"
- Problem: 3, 7, 4 — the assistant's author↔URL pairings are swapped relative to the summary it copied from, and the summary's own attributions are wrong anyway (PMC2916135 is Chalaris et al., J Exp Med 2010; PMC4816818 is a 2016 paper). The garbled "ovhangir" was silently repaired to "Blaydon et al. 2011" from memory — correct, but unlabelled. No mouse study's design (hypomorphic allele, DSS model, n) is stated. A reader following these links gets the wrong paper.
- Severity: medium

- **[F8] session=8fcc1cb4 msg=47a7d09e-fbb9-4cd2-9d66-c10063904b1e time=2026-07-13 18:55:53 user=mjdaly backend=perplexity**
- Question: (same)
- Claim (verbatim): "This makes a **partial loss-of-function in ADAM17 sheddase activity** the most parsimonious unifying mechanism." / "The IBD-risk allele's association with *lower* shed TNF-R1 fits a **partial loss-of-function** model"
- Source (loaded RESULT, verbatim): `"clinvar": {"clinical_significance": ["Benign"], "conditions": ["NM_003183.6(ADAM17):c.844-5T>C"]}`
- Problem: 6 — the literature mechanism (biallelic complete LoF → NISBD; hypomorph mice → colitis) was transplanted onto a −5 intronic splice-region variant that the loaded annotation calls ClinVar-Benign. "Benign" appears in the Pass 1 table and is never mentioned again; the analysis does not say why a Benign intronic variant should be a partial LoF, nor that no sQTL was checked (it offered to). The pQTL artifact reasoning in the same message is careful (G7), which makes the unreconciled LoF story stand out.
- Severity: medium

- **[F9] session=625f5a33 msg=076f1537-ce2a-4814-b658-c915efc205ed time=2026-07-22 11:41:55 user=mjdaly backend=none**
- Question: "how many credible sets are at the GBA gene - can you summarize their content"
- Claim (verbatim): "**N370S (N409S)** — the classic 'mild' Gaucher/PD allele ... associated with ~2–3× PD risk. Matches the observed OR ≈ 2.3." / "**E326K (E365K)** — a 'risk-only' variant ... robustly increases PD risk (~1.5×). Matches OR ≈ 1.58." / "GBA1 is an active therapeutic target for PD (... have entered clinical trials)"
- Source: no tool call — model memory
- Problem: 7 — an entire "Pass 2 — Literature context" section with no citation, then used circularly to "match" the loaded ORs. The content is consistent with the literature, but a reader cannot tell it from a search result, and "robustly" is unanchored.
- Severity: low

- **[F10] session=f33e24ea msg=9019b359-3a7b-492b-be2a-01176c2b34c9 time=2026-07-13 12:21:19 user=mjdaly backend=perplexity**
- Question: "are there any genetic variants that affect the levels of more than 100 proteins?"
- Claim (verbatim): "The perplexity-backend literature summary confirms that such hotspots (classically *ABO*, *ASGR1*, *CFH*, *APOE*, *STAB1*) are a well-established feature" / "Note the well-known *ASGR1* locus (chr17:~7.1–7.2 Mb) appears here as `17:7166093` and `17:7203059`."
- Source: not persisted. The assistant's own interim note two paragraphs earlier: "its specific hotspot-gene claims should be treated as unverified."
- Problem: 4, 7, 6 — the gene list it just called unverified is presented as "classically" known in the answer. The ASGR1 attribution contradicts the assistant's own LD check seven minutes earlier (session e4c7aaba, msg 5a63960c: 17:7166093, 17:7176997 [ASGR1 synonymous] and 17:7203059 all pairwise r² < 0.1 → "three independent loci").
- Severity: low

- **[F11] session=9d6ddfb4 msg=03bb28e9-55aa-4f93-938f-77c4e848315a time=2026-07-12 19:44:05 user=mjdaly backend=perplexity**
- Question: gastric zymogens PGA3/4/5, PGC — GWAS and Mendelian evidence
- Claim (verbatim): "The backend **perplexity** explicitly noted it found **no well-powered GWAS linking PGA3/PGA5/PGC germline variants to gastric cancer or to serum pepsinogen *levels*** in its indexed sources." / Pass 3: "That claim rests on a regional candidate-gene study of modest power and, per Pass 2, has not been established by well-powered GWAS."
- Source (verbatim): "Based on the provided search results, there are **no published scientific papers** that directly investigate..." ... "To find the specific GWAS study you are looking for, you may need to search databases (e.g., PubMed, GWAS Catalog)"
- Problem: 4 — a retrieval miss ("in the provided search results") becomes "has not been established by well-powered GWAS"; the summary itself said to search elsewhere and no second search was run. "Modest power"/"small-cohort" for PMC5707023 is inferred (the summary gives no n) and stated as fact — the right instinct (G6) with invented specificity.
- Severity: low

- **[F12] session=eff4a3c7 msg=1754c2d6-5d91-4dfa-a528-852a7daa410d time=2026-07-08 01:55:14 user=mjdaly backend=perplexity**
- Question: "which of these genes have the highest expression ... in immune cells"
- Claim (verbatim): "**IRF8, CEBPE, MAF (MafB)** are core **myeloid** transcription factors — ... MafB is myeloid-lineage-specific (Perplexity backend synthesis, citing e.g. [Kurotaki et al. 2014, PMID 25236377] ...)" / "The Human Protein Atlas immune-cell atlas ... confirms **MNDA** and **IRF8** as immune-cell-enriched"
- Source: not persisted
- Problem: 3, 7 — MAF (c-Maf, on the user's list) and MAFB are different genes; the literature claim is about MafB and was applied to MAF, which then carries into the ranking ("MNDA, IRF8, CEBPE, MAF, LITAF (myeloid)"). The HPA "confirms" comes from a `proteinatlas.org` web search whose content is not shown, in a session where the assistant later says HPA values were not retrievable through its tools.
- Severity: medium (the answer is a ranking, and a gene was ranked on the wrong gene's literature)

## 3. Counter-examples

- **[G1] session=3cc21a7a msg=ac8e15df-59eb-4763-b853-d27a4d4be3a4 time=2026-07-09 03:05:39 backend=perplexity** — "The literature backend queried was `perplexity` (the `europepmc` call routed to a Perplexity summary — I flag that explicitly below)"; the citation is labelled "(bioRxiv 2020, ...)"; and the locus-vs-gene distinction is drawn: "'phenotypic associations at the locus' is not the same as 'associations caused by GATA2.'" Partial: preprint status noted but not acted on (F1).
- **[G2] session=3cc21a7a msg=cf6c44a8-3817-449d-938c-92f365a8407a time=2026-07-09 12:27:52 backend=perplexity** — "⚠️ Backend note ... the citations below are Perplexity-sourced PMC links, not structured Europe PMC records." Offers the right test: "I'd recommend querying rs1131665 directly in a lupus-powered GWAS ... Would you like me to pull rs1131665 directly". Partial: the offer is not carried out before the confident conclusion (F2).
- **[G3] session=3cc21a7a msg=7b1ad195-54fa-4113-b6b5-ab6eb8ded728 time=2026-07-09 12:48:03 backend=perplexity** — "(Note: the perplexity summary flagged rs9743723 as the *regulatory* lead in one Hispanic cohort — but our fine-mapping identifies a *missense* CEBPE variant at PIP 1.0 in FinnGen, a stronger causal assignment.)" — literature compared against loaded data and ranked below it.
- **[G4] session=1b236387 msg=76abf7d1-644e-464a-93ff-d6eca39508b0 time=2026-07-12 14:34:33 backend=none** — after user pushback, the literature-derived mechanism is withdrawn on the data: "my Pass-3 statement in the previous turn — that the common cortisol association acts simply by raising the *amount* of CBG protein — is **not supported by the colocalization/LD data**. You correctly spotted this." Also flags "pQTL 'protein level' from affinity assays can reflect epitope/binding effects".
- **[G5] session=1b236387 msg=15853e89-41e6-47c2-b1af-9323c6b4e0cd time=2026-07-12 14:38:44 backend=perplexity** — "*Caveat: these citations come from the perplexity backend's AI summary and lack the structured metadata (titles/authors/PMIDs) ... the specific statistics (rs2749527, the coloc posterior) should be verified against the primary Cell Genomics paper before being treated as established.*" The best explicit literature caveat in the file — undone by the bottom line (F5).
- **[G6] session=9d6ddfb4 msg=03bb28e9-55aa-4f93-938f-77c4e848315a time=2026-07-12 19:44:05 backend=perplexity** — PMC5707023: "This is a **regional candidate-gene / small-cohort study, not a well-powered GWAS**, and remains to be robustly replicated at genome-wide significance." and "the literature's disease claim is, at present, **unconfirmed** by fine-mapped GWAS here rather than contradicted." Also distinguishes "expression evidence, not germline disease-variant evidence".
- **[G7] session=8fcc1cb4 msg=47a7d09e-fbb9-4cd2-9d66-c10063904b1e time=2026-07-13 18:55:53 backend=perplexity** — "Shared lead-variant identity is strong evidence ... though it is weaker than a formal coloc statistic — I'm flagging that distinction explicitly." ERBB4/NTRK3/AMIGO2 pQTLs called "the hallmark of a **binding/epitope artifact**"; MGI used as independent corroboration of the literature's TNF claim. Scrutiny of loaded data, not of the literature (see F7/F8).
- **[G8] session=43d0e878 msg=67f6f1b5-dcea-482b-8cae-35b41c9b002e time=2026-07-24 15:36:52 backend=perplexity (not persisted)** — "[PMID 29855681]: ... (Candidate-gene study, single population — modest evidence.)"; "structured fields were empty, so treat specific claims with appropriate caution and verify primary sources"; and in Pass 3: "The candidate-gene report ... is a single-population study and is *not* corroborated as a fine-mapped coding signal in the large IIBDGC/FinnGen data here." Every citation carries a PMID. This is the baseline good behaviour.

Frequency: of the 11 tool-backed literature messages, 5 contain a real scrutiny statement (G3, G5, G6, G8, plus G1/G2's backend flags), i.e. roughly half — and in three of those (G1, G2, G5) the caveat does not survive into the conclusion.

## 4. Patterns

1. **The europepmc backend never delivered.** All five persisted `backend="europepmc"` requests returned `"source": "perplexity"` with empty title/author/pmid fields. The assistant noticed every time and said so — then used the AI summary as its literature pass anyway. In 15 sessions no primary paper was ever opened; every "Pass 2 — Literature" is a re-styling of the Perplexity summary (its bold "robustly associated", "confirms", "explicitly investigates" language survives verbatim into the assistant's text: F1, F2, F5). Where the summary is wrong or hedged ("Likely" DOI, garbled author names, a "posterior" of 1e-15, swapped citation numbers), the assistant transmits it (F5, F7).

2. **There is a genetics-evidence rubric and no literature-evidence rubric.** Unprompted, every message grades loaded results by PIP, CS size, pseudo-CS status, LD, coloc PP.H4, allele frequency and epitope-artifact risk. Literature bullets carry none of that: no n, design, replication, effect size or preprint/review status unless the Perplexity summary happened to include it. The user's complaint is exactly this asymmetry, and F2 shows it inside a single message ("PIP 0.024 in a 56-variant credible set" next to "well-replicated, mechanistically-anchored").

3. **Scrutiny happens only where there is loaded data to compare against.** The counter-examples (G3, G6, G8) are all "the literature says X, our fine-mapping shows Y". Claims with no loaded counterpart — a mouse hypomorph phenotype, a candidate-gene SLE variant, a preprint's histology remark — pass through unexamined. When loaded data and literature disagree, the assistant sides with whichever agrees with the loaded data at that moment rather than reconciling the two literature claims (F4 vs F5).

4. **Model memory is an unlabelled third source.** Allele names (Pi*Z, W262R, L446P), author-year citations (Witt 2006, Rosendahl 2018, Blaydon 2011), ORs (GBA), and "well-known hub loci" appear without any tool result, mixed into sections headed "Literature". Memory content was accurate in this sample, but the reader cannot separate it from search output (F6, F9), and one memory slip (MAF vs MafB, F12) fed a ranking.

5. **The response template compresses away the caveats.** Caveats live in Pass 2; the bottom line restates the claim without them (F1 preprint, F5 "verify against the primary paper" → "explicitly fails to colocalize", F10 "unverified" → "classically"). The bottom line is what the user reads.

6. **What would fix it, in the assistant's own vocabulary:** treat a Perplexity summary as an index, not a source — open at least one primary link before quoting a number; require for every literature claim the same fields it already requires for a credible set (study type, n, replication, effect size, preprint/review flag); label memory-derived statements as such; and never let "confirms / robustly / well-replicated" pass from summary to answer without a number attached. Category 6 (unreconciled contradiction) is cheap to fix — when the loaded data disagrees with a paper, say which one you believe and why, before the user has to.

## Borderline cases skipped (5)

- 33d29846 msg 9f71c66f: "well-documented feature of the 9p21 CAD locus ... thought to act largely through *ANRIL*" — uncited memory, mainstream view; skipped as too generic to be misleading.
- e4c7aaba msg c485d738: allele names (GCKR L446P, SH2B3 W262R, SERPINA1 Pi*Z region, ASGR1 12-bp deletion) from memory — correct; counted in the no-tool-call tally, not a finding.
- 3f78b679 msg 47d348ea: "SH2B3 (LNK), GP6, SLC22A4 and JMJD1C are all established regulators of platelet and blood-cell counts" — uncited but well-known; skipped.
- 3cc21a7a msg d7d65dc1: Kanai et al. medRxiv cited as "the source dataset" via web_search — preprint status visible in the link, used only for dataset description; skipped.
- 9d6ddfb4 msg 03bb28e9: "PMC5707023 ... small-cohort" inferred without an n — folded into F11 rather than filed separately.
