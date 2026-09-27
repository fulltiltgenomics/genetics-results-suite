# Literature-scrutiny review: mjdaly-4.txt + mjdaly-5.txt

Scope: 16 sessions (13 in mjdaly-4, 3 in mjdaly-5), user=mjdaly, 2026-08-07 to 2026-09-25. Every
assistant message was read; 15 assistant messages report literature. Five of those (the
`*[Using tool: ...]*` format: 5ff4e634, e7f34ff3, f5497164, bccfab9b, 74694be9) have no RESULT
blocks, so they were judged from the assistant's text alone. In the persisted messages the
RESULT blocks are truncated after roughly 5–10 of the 8–20 returned records, so several cited
papers sit in the hidden tail and could not be checked either way.

## 1. Counts

| item | count |
|---|---|
| `search_scientific_literature` calls, backend=perplexity | 32 |
| `search_scientific_literature` calls, backend=europepmc | 0 |
| `web_search` calls | 0 |
| assistant messages reporting literature | 15 |
| ...of which results not persisted | 5 |
| messages citing literature with **no** literature tool call | 0 |
| messages with a tool call that also blend uncited memory claims into the cited material | 3 (5ff4e634, f303253d, b451eca0) |
| findings (F) | 6 |
| counter-examples (G) | 15 |
| user pushback on a literature claim | 0 (the one pushback, 2f6c6b26 "signals I expected to find... PCSK7, CLEC7A", disputes the data query, not a paper) |
| borderline cases skipped | 6 (see end of §2) |

Findings by rubric category (a finding may hit several):

| cat | 1 face-value | 2 tier mismatch | 3 source≠claim | 4 Perplexity-as-source | 5 candidate-gene | 6 lit vs data unreconciled | 7 provenance | 8 review-as-evidence |
|---|---|---|---|---|---|---|---|---|
| n | 1 | 1 | 3 | 1 | 0 | 0 | 5 | 1 |

Per-call breakdown (all perplexity): d29c3eb7 5 · 4c8d711a 6 · 5fa5ef7b 1 · d269cf98 5 · 4476c0ea 1 · b0377ca6 4 · 7f937cbf 3 · 3467e11d 2 · 2ee7b2fc 1 · 6bb766f6 3 · 2b4114d0 1. Sessions with no literature call: bbcc355a, 2a907ba6, 10ebe7bf, fc400c00.

## 2. Findings

- **[F1] session=d269cf98 msg=f303253d-caf5-471f-9160-863c8f39a638 time=2026-08-19 16:26:52 user=mjdaly backend=perplexity**
- Question: "could you scan these genes to see which might have a role in wound healing?"
- Claim (verbatim, Tier B table): "uPAR is required for keratinocyte migration and EGFR-dependent laminin-5; PLAUR is among 22 genes upregulated in human skin re-epithelialisation ([Ågren et al. 2022]...)" / "CD100–plexin-B2 controls γδ T-cell/keratinocyte cutaneous repair; myeloid plexin-B2 drives wound corralling/compaction ([Zhou et al. 2020]...)" / "P-selectin sits in the platelet/leukocyte-recruitment phase of cutaneous repair ([Opneja et al. 2019]...)"
- Source (verbatim from RESULT): Zhou 2020: "Tissue repair after spinal cord injury requires the mobilization of immune and glial cells... Plexin-B2 deletion in myeloid cells impairs corralling" (mouse SCI). Roney 2011 (same RESULT, not cited): "Plxnb2(-/-) macrophages demonstrate faster in vitro wound closure activity." Opneja 2019 abstract: review of "platelets, the coagulation and fibrinolytic systems" — P-selectin not mentioned. No returned record contains "EGFR-dependent laminin-5" or "CD100".
- Problem: 3, 7, 8, 2 — the first clause of each row is model memory welded to a search citation that does not contain it; the PLXNB2 citation is mouse spinal cord, and the retrieved Roney paper points the opposite way (faster closure on loss) without being mentioned; a review is cited as evidence for a specific molecule it does not name. Contrast with the genetics in the same message: "all effects are IIBDGC multi-ancestry pseudo credible sets (single meta-analysis, not fine-mapped)... note PIP is low and this is a rare stop" — the loaded data get design caveats, the Tier-B literature gets none (no species, no n, no assay).
- Severity: medium (a prioritisation table the user thanked as "a great list" and was going to act on).

- **[F2] session=b0377ca6 msg=b451eca0-1830-46fd-b7ef-afdba8f1e7d8 time=2026-09-04 14:17:14 user=mjdaly backend=perplexity** (paired with session=4476c0ea msg=f5497164-cee6-4653-952a-241f1936961d time=2026-09-03 20:58:07, results not persisted)
- Question: pairwise protein sharing / directional concordance among 26 lead pQTL variants.
- Claim (verbatim): "[Eldjarn et al. 2023] ... **46 Olink and 35 SomaScan pQTLs each associated with >50 proteins.**" and "[Sun et al. 2018] ... hotspots affecting >20 include **ABO, CFH, APOE, KLKB1 and ZFPM2** — four of which correspond to variants in my set". The day before (f5497164) the same paper was summarised as "trans 'hotspot' regions including **ABO, CFH, APOE, KLKB1**, and a **SH2B3–BRAP** trans-pQTL."
- Source (verbatim from RESULT): Eldjarn abstract (fully visible): "cis protein quantitative trait loci were detected for a similar absolute number of assays on the two platforms (2,101 on Olink versus 2,120 on SomaScan)... (72% versus 43%)" — no 46/35 figure. Sun 2018 abstract (fully visible): "We identify 1,927 genetic associations with 1,478 proteins... including trans associations for 1,104 proteins" — no hotspot gene list.
- Problem: 7, 3, 4 — specific numbers and gene lists are attributed to structured records that do not contain them, in a message whose own caveat says "I have cited only the structured records"; the Sun 2018 hotspot list changes between two messages and gains ZFPM2 exactly when ZFPM2 is in the assistant's own top-26 (either memory or the unshown Perplexity narrative). Not verifiable, not flagged as such.
- Severity: low (decorative; the analysis's conclusions rest on the data, not these numbers).

- **[F3] session=6bb766f6 msg=c392209f-b4a9-41d1-9d65-8881bc7c0d39 time=2026-09-25 14:15:11 user=mjdaly backend=perplexity**
- Question: does chr2:9521321:A:G (ADAM17) colocalize with molecular QTLs and what process is implicated?
- Claim (verbatim): "[Veit et al. 2019] ... Sheddase-dead ADAM17 abolishes PMA-stimulated substrate release, including soluble TNFR1." and "TNFR1 (TNFRSF1A) as an ADAM17 substrate is well documented in the results above."
- Source (verbatim from RESULT): Veit 2019: "PMA-stimulated release of ADAM17 substrates was completely abolished." Le Gall 2009: "principal sheddase for transforming growth factor (TGF)-alpha and heparin-binding epidermal growth factor". No visible record names TNFR1/TNFRSF1A. Tang 2020 (cited) is not in the visible records.
- Problem: 3, 7 — the substrate identity that carries the mechanistic argument (five trans-pQTLs read as shed ectodomains) is true from background knowledge but is presented as documented by the retrieved records, which it is not. The assistant was careful about RANK/NTRK3/AMIGO2 ("I did not find primary literature...") but not about TNFR1.
- Severity: low.

- **[F4] session=d29c3eb7 msg=c5bfd29e-efe5-4b0e-ab6a-af69f918539b time=2026-08-07 19:48:45 user=mjdaly backend=perplexity**
- Question: "should warfarin lower F10 levels?"
- Claim (verbatim): "Antigen falls partly because uncarboxylated FX precursor is retained in the ER and secretion is impaired, not only because it's non-functional ([Stanton & Wallin 1992]...)."
- Source (verbatim from RESULT): "we have followed the factor X precursor through the secretory pathway in **rat liver**... Warfarin administration resulted in accumulation of factor X"
- Problem: 1 — stated as a human-plasma mechanism with no species; in the same message the rat and rabbit studies are labelled ("In rats...", "Rabbit work shows...") so this one row is the exception to an otherwise careful answer.
- Severity: low.

- **[F5] session=d29c3eb7 msg=5ff4e634-2b54-4aac-b01b-89e2375ff967 time=2026-08-07 19:01:33 user=mjdaly backend=perplexity**
- Question: "are there any phenotypic associations which strongly implicate GPA33"
- Claim (verbatim): "So the gene has a clear barrier/colitis phenotype in mouse and is an established colorectal-cancer antibody target (A33), but human common-variant genetics here gives it no disease anchor."
- Source: not persisted (one perplexity call; the Williams 2015 DMM knockout claim is cited with a PMC link; the A33 antibody-target claim carries no citation).
- Problem: 7 — a "from memory" fact blended into a paragraph that otherwise cites its source; the reader cannot separate the two.
- Severity: low.

- **[F6] session=2b4114d0 msg=eac0e57f-c5ea-4101-9a17-e38a9df52795 time=2026-09-25 18:34:24 user=mjdaly backend=perplexity**
- Question: scatter of UC vs MUCOPROCT −log10 p at UC lead variants.
- Claim (verbatim): "in FinnGen its β is ~0.5–0.6 (OR ≈ 1.65–1.83), somewhat smaller than the OR 2.19 in Luo et al. 2017, but the CIs overlap."
- Source (verbatim from RESULT): Luo 2017 abstract: "We discovered a 0.6% frequency missense variant in ADCY7 that doubles the risk of ulcerative colitis." No OR or CI in any returned record; the Perplexity summary is truncated.
- Problem: 7 — a precise OR and a CI-overlap statement attributed to a paper whose retrieved text gives neither; the comparison with the loaded FinnGen estimate (a good instinct) is anchored on an unverifiable number.
- Severity: low.

Borderline cases skipped (6): (a) 540a0554 "Dhindsa... STAB1 and STAB2... 77 and 41 protein associations", "Suhre 2024... 7.6-fold enriched", and "Sigurdsson et al. 2025, Nat Commun" where the visible record is a 2024 medRxiv preprint (`is_preprint: true`) — all three could sit in the truncated tail of a 10-record result, so not verifiable; (b) fbd27319 quotes Degenhardt 2021 ("such delineation could not be made because of tight structures of linkage disequilibrium") whose abstract is cut off in RESULT, and cites the PMC version while one returned record is the medRxiv preprint; (c) fbd27319 does not note that the cited Ashton 2019 review names "HLA-DRB1*03:01 the most associated risk allele" whereas Goyette 2015 (also cited) gives "a primary role for HLA-DRB1*01:03" — a source-vs-source discrepancy left unflagged, but the assistant used Ashton only for the LD point; (d) 0ee4f85a and e7f34ff3 cite ~15 papers not visible in the truncated/absent RESULTs (Raess 2021, Godschalk 1988, Kuroki 2008, Thiele 2019, Kawazoe 2018, Enroth 2018, Hillary 2024, McGurk 2025...) — the n's and settings are stated, so nothing to fault, but nothing to verify.

## 3. Counter-examples

- **[G1] session=d29c3eb7 msg=c5bfd29e-efe5-4b0e-ab6a-af69f918539b time=2026-08-07 19:48:45 user=mjdaly backend=perplexity**
- Question: "should warfarin lower F10 levels?"
- Claim: "In rats, FX activity has one of the shorter half-lives... ([Vainieri & Wingard 1977])" / "In 100 samples from long-term warfarin patients, both activity *and* antigen of II/VII/IX/X fell ([Takahashi et al. 1986])" / "Rabbit work shows depression of FX (to ~14%)..."
- Source: Vainieri: "in individual rats after acute and chronic dosing"; Takahashi: "measured in 100 specimens from patients on long-term warfarin therapy" — match.
- Good behaviour: species and n stated per citation; ends "I don't have any data here quantifying that shift."

- **[G2] session=d29c3eb7 msg=0ee4f85a-ec78-4e49-9c43-8e6c8cefbdc0 time=2026-08-07 19:52:26 user=mjdaly backend=perplexity**
- Question: plasma proteins elevated/reduced by glucocorticoids.
- Claim: "Directions below are from human studies; I've flagged where the evidence is animal, in vitro, or contradictory... these papers report percent changes rather than β ± SE" / "prednisone, healthy men, n=7 ([Godschalk 1988])" / "([Kawazoe 2018]) — directly contradicts the cross-sectional sclerostin result above" / "Serum albumin/total protein increases are rat-only" / "**Not found in these searches:** corticosteroid-binding globulin (SERPINA6) and ACTH".
- Source: Hathout 2016 and Roy 2022 abstracts match the table rows; the others are in the truncated tail.
- Good behaviour: n, design, direction conflicts and search gaps stated explicitly; a negative ("not found") is reported rather than filled from memory.

- **[G3] session=4c8d711a msg=e7f34ff3-b48a-47e0-a59a-5ac865b478f3 time=2026-08-11 16:51:54 user=mjdaly backend=perplexity**
- Question: studies of systematic medication effects on the plasma proteome.
- Claim: "Sample size not stated in the abstract I retrieved." / "The obvious limitation of everything in the top table is confounding by indication... none of the observational studies use an active-comparator or trial-emulation design. Effect sizes with standard errors are not in the abstracts I retrieved"
- Source: not persisted.
- Good behaviour: study design critique applied to every row; declines to invent n or SE.

- **[G4] session=5fa5ef7b msg=fbd27319-33a6-43f6-9e55-b66e31c936cb time=2026-08-12 11:02:29 user=mjdaly backend=perplexity**
- Question: SNPs linked to DRB1*01:03.
- Claim: "[Vestergaard et al. 2024] OR 3.32 (95% CI 2.25–4.89), p = 1.45e-9 for severe UC, carriage 2.8% in Danish cohorts — ~12× the Finnish carriage rate." / "[Goyette et al. 2015] needed high-density MHC SNP typing plus imputation in >32,000 IBD cases"
- Source: Vestergaard: "OR, 3.32 [95% CI, 2.25-4.89]; *P* = 1.45 × 10^−9^... carriage of allele in cohorts = 2.8%"; Goyette: ">32,000 individuals with IBD" — exact match.
- Good behaviour: numbers transcribed with CI, and reconciled against the loaded FinnGen allele frequency (0.113%, info 0.30).

- **[G5] session=d269cf98 msg=f303253d-caf5-471f-9160-863c8f39a638 time=2026-08-19 16:26:52 user=mjdaly backend=perplexity**
- Question: wound-healing role of the 94 IBD coding-variant genes.
- Claim: "MSP function of R689C is contested: ~10-fold reduced RON affinity in vitro ([Gorlatova 2011]), normal binding but **lower serum MSP** in carriers ([Kauder 2013]), versus a reported gain of function in THP-1 ([Genes Immun 2012])." / "the perplexity backend returned **no direct primary paper** for any of the three. Biologically plausible, currently unsupported by what I retrieved." / "**No gene of the 94 carries an MGI wound-healing annotation.**"
- Source: Gorlatova "affinity to RON is approximately 10-fold lower"; Kauder "The 689C polymorphism had no impact on the ability of MSP to bind... carriers... had lower concentrations of MSP"; Genes Immun snippet "gain of function" — match.
- Good behaviour: three conflicting functional papers presented as a conflict; explicit negatives; Tier A/B separation of "variant itself tested" vs "gene-level literature". (Same message as F1 — the Tier B rows are where it slipped.)

- **[G6] session=4476c0ea msg=f5497164-cee6-4653-952a-241f1936961d time=2026-09-03 20:58:07 user=mjdaly backend=perplexity**
- Question: UKB-PPP loci with >100 colocalized proteins.
- Claim: "Sun et al. 2023 (Pass 2) report **16 extremely pleiotropic loci with ≥100 proteins**... I find **48** (Pass 1.1). This is a definition difference, not a contradiction" / "**These are confirmation, not discovery.**" / "Effects of ~1.2 SD with SE 0.004 at a common variant, on the protein whose own coding sequence is altered, are the signature Eldjarn et al. (Pass 2) attribute at least partly to **antibody/aptamer epitope effects**"
- Source: not persisted (the "16 loci... at least 100 proteins" and "72% versus 43%" figures are confirmed by RESULTs in the next session).
- Good behaviour: literature count reconciled with the loaded data rather than either being trusted; literature used to down-weight the assistant's own strongest cis effects.

- **[G7] session=b0377ca6 msg=540a0554-5a4c-423a-b019-ddc6f9f8272d time=2026-09-04 00:50:18 user=mjdaly backend=perplexity**
- Question: SNPs in credible sets for >100 proteins, LD-pruned.
- Claim: "Caveat on the Perplexity output: its own summary table mis-attributes several records (e.g. it lists 'Genomic atlas of the human plasma proteome' as Cell 2019 while the structured record correctly says Nature 2018...). I have cited only the structured records, not the summary's attributions. I did not find a paper specifically quantifying epitope-binding artefacts for these exact loci — the epitope claim appears only in the Perplexity narrative, unsupported by any returned record, so I am not asserting it."
- Source: structured record "Genomic atlas of the human plasma proteome. ... year: 2018 ... pmid: 29875488".
- Good behaviour: the exact behaviour the user's complaint asks for — Perplexity's narrative treated as untrusted, structured records as the source, and a claim dropped because no record supports it. Also "42,853 here vs 54,219 in the published Sun et al. 2023 paper — this is a FinnGen-side reprocessing".

- **[G8] session=b0377ca6 msg=90ac7dad-fd7f-49fb-8700-cb6f2f921d2f time=2026-09-04 01:30:59 user=mjdaly backend=perplexity**
- Question: why PCSK7 and CLEC7A were missing.
- Claim: "**PCSK7 is not named in any returned record** — the Perplexity narrative said so itself. And the narrative invented a paper titled *'Integration of biobank-scale genetics and plasma proteomics identifies trans-pQTLs for CLEC7A'* claiming a CLEC7A–CSF3 colocalization; no such record was returned. The real underlying record is Donoghue et al. 2025, whose abstract makes the different and weaker claim above. I have not used the fabricated title or the CSF3 claim as evidence."
- Source: Donoghue abstract: "70 proteins with putative causal roles in asthma risk, including... CLEC7A" — matches the "weaker claim".
- Good behaviour: a fabricated citation caught and named; the user's expectation traced to a transposed reading of a real paper ("CLEC7A as a pQTL *target*, not a pleiotropic locus").

- **[G9] session=b0377ca6 msg=b451eca0-1830-46fd-b7ef-afdba8f1e7d8 time=2026-09-04 14:17:14 user=mjdaly backend=perplexity**
- Question: pairwise concordance heatmap.
- Claim: "**No returned record reports a pQTL-by-pQTL pairwise directional-concordance matrix**, so the specific statistic computed here has no direct published comparator that I found." / "Li et al. 2026 (Pass 2) explicitly account for blood cell composition before calling trans-pQTLs, which suggests this is a recognised confounder rather than a novel network"
- Source: Li 2026 abstract: "we identified 17,662 trans-pQTLs affecting 961 PPI clusters after accounting for blood cell composition effects" — match.
- Good behaviour: absence of a comparator stated; a paper's methods choice used to down-weight the assistant's own Module I. (Same message as F2.)

- **[G10] session=7f937cbf msg=bccfab9b-5588-4609-870a-737249e08933 time=2026-09-12 17:58:49 user=mjdaly backend=perplexity**
- Question: medical consequence or biological role of SHISAL1.
- Claim: "[Pei & Grishin 2012] ... This is a **sequence/comparative-genomics inference, not experimental characterisation of SHISAL1.**" / "[Klaassen 2016]... **Shisa6, not SHISAL1.**" / "[Klein et al. 2019, J Dairy Sci]... the perplexity summary asserts it mentions a human 'obesity-related renal cell carcinoma' link for KIAA1644. **I could not verify this claim in the retrieved abstract, and it is a single passing annotation mention in a cattle paper**" / "These are IMPC high-throughput screen calls from a single allele — broad-screen annotations, not deep characterisation, and I have no effect sizes or n from MGI."
- Source: not persisted.
- Good behaviour: every record classified by what it can and cannot say; Perplexity's own gloss rejected; family-level inference refused ("Transferring Shisa6 biology to SHISAL1 on the basis of a shared cysteine pattern would be unjustified").

- **[G11] session=3467e11d msg=74694be9-285f-45f8-840b-76d6d7e9ff5b time=2026-09-15 13:29:20 user=mjdaly backend=perplexity**
- Question: evidence for APBB2 in Alzheimer's disease.
- Claim: "[Li et al. 2005] — three case-control series, ~2,000 samples... p = 0.041... Stronger in onset <75 years (OR_hom 2.43, p = 6×10⁻⁵) — a post-hoc age stratification." / "[Golanska et al. 2008] — Polish sample, 213 cases / 171 controls... p = 0.03 (subgroup, uncorrected)." / "The review by [Giri et al. 2017] cites APBB2 solely on the basis of Li et al. 2005." / "**The original candidate-gene result is directly refuted, not merely unreplicated**... more than 20 standard errors from the reported effect." / "the cortical-lamination phenotypes are the kind reported for double mutants... treat them as evidence for a neurodevelopmental role of the Fe65 family, not specifically for single-gene Apbb2 loss."
- Source: not persisted.
- Good behaviour: the model answer for categories 5, 6 and 8 — n, uncorrected p, post-hoc stratification named; the review traced to its single primary; the literature SNPs pulled into FinnGen and the CI contrast computed; the NCBI Gene summary sentence explicitly down-weighted.

- **[G12] session=2ee7b2fc msg=a8a081fa-350c-4b6d-8494-40b4fcab152d time=2026-09-23 13:29:41 user=mjdaly backend=perplexity**
- Question: credible associations for 6-160585045-C-T.
- Claim: "[Mack et al. 2017]... the perplexity summary attributes to this paper the statement that rs41272114 sits at a splice site and causes null alleles (not verifiable from the abstract alone)." / "The perplexity summary also quotes a per-allele CHD OR of 0.88 (0.84–0.93, p = 3.4e−7) for rs41272114 from the Emdin work; that figure is from the AI summary rather than a retrieved abstract, so treat it as unverified here."
- Source: Emdin abstract gives only "One SD genetically lowered Lp(a) level was associated with a 29% lower risk of CHD (odds ratio [OR]: 0.71; 95% confidence interval [CI]: 0.69 to 0.73)" — the assistant reported exactly that and quarantined the per-allele OR.
- Good behaviour: AI-summary numbers separated from abstract numbers, twice, in-line.

- **[G13] session=6bb766f6 msg=c392209f-b4a9-41d1-9d65-8881bc7c0d39 time=2026-09-25 14:15:11 user=mjdaly backend=perplexity**
- Question: ADAM17 variant mechanism.
- Claim: literature table carries a **Design** column: "Family study, 2 sibs" / "Case report" / "Mouse" / "Review"; "[Gettler et al. 2021]... p.Val673Ile in CD cases (OR 1.76 in the Perplexity summary — I have not verified that number against the paper)." / "I did not find primary literature establishing RANK, NTRK3 or AMIGO2 as ADAM17 substrates, so for those three the inference rests on the genetics, not on prior biochemistry."
- Source: Blaydon 2011 "a sister and brother"; Samuelov 2022 "we report on a young female individual"; Fuchslocher Chico 2018 "ADAM17 hypomorphic (ADAM17ex/ex) mice" — match.
- Good behaviour: the only message in the set where every citation carries its study design; unverified summary numbers labelled. (Same message as F3.)

- **[G14] session=2b4114d0 msg=eac0e57f-c5ea-4101-9a17-e38a9df52795 time=2026-09-25 18:34:24 user=mjdaly backend=perplexity**
- Question: UC vs MUCOPROCT scatter.
- Claim: "[Luo et al. 2017] — low-coverage WGS of 4,280 IBD patients imputed into 16,432 cases / 18,843 controls; discovered a 0.6%-frequency *ADCY7* missense variant" / "[Cardinale et al. 2025] — cell-based functional work" / "I did not search the *IFIH1*, *ATG5* or chr22 loci; all three are established IBD/UC GWAS loci in the literature I am aware of, but I have not verified that here."
- Source: Luo: "sequenced the whole genomes of 4,280 patients at low coverage... 16,432 cases and 18,843 controls... a 0.6% frequency missense variant" — match.
- Good behaviour: search scope and memory-only claims labelled as such; the paper's n reproduced. (Same message as F6.)

- **[G15] session=7f937cbf msg=fd2f4c7b-de8a-4100-89f1-0a30ed0f3282 time=2026-09-12 18:03:04 user=mjdaly backend=perplexity**
- Question: molecular evidence for 22-44282480-G-GA.
- Claim: "[ClinVar variation 2528632] A *different* SHISAL1/KIAA1644 variant at 22:44,285,598 (GRCh38), classified **uncertain significance**, no functional evidence attached. Not rs11382190" / "The perplexity summary states explicitly that it found **no primary research article reporting an association for rs11382190**. That matches my own reading of the returned records"
- Source: Perplexity summary: "I **did not find a primary scientific paper** in the supplied results that clearly studies **rs11382190**"; ClinVar record: "Cytogenetic: 22q13.31 22: 44285598 (GRCh38)".
- Good behaviour: a near-miss ClinVar record explicitly disqualified; the negative reported as a negative.

## 4. Patterns

1. **Backend.** Every one of the 32 literature calls went through Perplexity; europepmc was never selected. The persisted results show what that costs: the Perplexity narrative mis-dated papers, mis-assigned authors, invented a paper title (G8), and supplied per-allele ORs and mechanism statements that no returned abstract contains (G12, F6 probably). The assistant caught this in four messages (G7, G8, G9, G12) — always by falling back on the structured records with PMIDs/DOIs. So the structured record, not the narrative, is the usable product; the narrative is the vector for exactly the "regurgitates rather than scrutinizes" failure the user describes.

2. **The failure mode here is provenance blending, not credulity.** In this user's transcripts the model rarely states a weak paper's conclusion as fact (cats 1, 5, 8 nearly empty); what it does is attach a background-knowledge sentence to a search citation that does not contain it (F1 PLAUR/PLXNB2, F2 Eldjarn 46/35 and the shifting Sun 2018 hotspot list, F3 TNFR1, F6 OR 2.19). The reader cannot tell which clause came from the record and which from the model, which is category 7 in the rubric and is the root of the "trusts at face value" impression: a memory claim inherits the citation's authority.

3. **Scrutiny is question-shaped.** When the question is "is gene X involved in disease Y" the model applies a full evidence-tier rubric to the literature — candidate-gene era, n, uncorrected p, post-hoc strata, review traced to its primary, mouse single-allele caveat (G10, G11). When the literature is used as mechanistic decoration inside a data table (F1 Tier B; the pQTL "Pass 2" tables) the per-citation caveats drop to zero and the study design is not stated. The genetics side never loses its caveat block ("pseudo credible sets", "SE not in data", "Finnish panel applied to British fine-mapping") because that block is evidently prompted; the literature side has no equivalent mandated field. The one message with a **Design** column per citation (G13) is the only one where nothing had to be inferred by the reviewer.

4. **Truncation defeats auditability.** RESULT blocks are cut after ~5–10 records while the assistant cites papers from the hidden tail (0ee4f85a cites ~13 papers of which 5 are visible; 540a0554 three of its seven). A reviewer — and presumably the assistant itself on a second pass — cannot check those claims. This is a persistence/format problem rather than a model problem, but it is where a "source does not support claim" audit stops being possible.

5. **What generalises as a fix.** (a) Prefer or force the europepmc backend for anything that will be cited, and treat the Perplexity `summary` field as untrusted text (the model already does this when it remembers to; make it a rule). (b) Require a per-citation design/n/species/replication field wherever a paper is cited, the same way `pseudo credible set` and `SE not in data` are already required for the loaded results — the F1-type row would then have had to say "mouse spinal-cord injury, n not stated" and the mismatch would have been visible. (c) A rule that any number, gene list or quotation attributed to a paper must appear in the returned record, otherwise it is labelled "from memory" — that single rule covers F2–F6. (d) Persist full literature RESULTs; the schizophrenia/demyelination case the user complains about is not in these transcripts, and with truncated results it could not be audited if it were.
