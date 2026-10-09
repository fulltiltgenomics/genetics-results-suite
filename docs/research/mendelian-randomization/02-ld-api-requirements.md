# What the Mendelian randomization module needs from the LD API

For the implementer of signed LD in the FinnGen LD server (`https://api.finngen.fi/api/ld`,
the upstream that results-api's `/v1/ld/{variant}` proxies and that `genetics.ld(...)`
reaches from the sandbox). This lists the contract the MR module will be written against;
how the server computes it is the implementer's choice, as long as the semantics below
are stated in the response or the API docs.

## Why signed r

The estimator for several correlated instruments in one gene region (generalized IVW,
`MendelianRandomization::mr_ivw(correl=…)`, and the principal-component variants) needs
the matrix of **signed** correlations between the instruments' effect alleles. r² and D'
cannot give the sign, and a matrix thresholded on r² is not positive definite, so neither
the existing per-variant neighbourhood nor a pairwise r² lookup can feed it. Pruning to
independent instruments (r² < 0.01) and proxy search (r² > 0.8) work with r² alone and
already do.

## 1. Signed r on every LD entry

Add `r` beside `r2` and `d_prime` in each `ld` entry the existing endpoint returns, with:

- **Definition stated once in the API docs**: Pearson correlation of the alt-allele
  dosages of `variation1` and `variation2` across the panel's samples (what `plink --r`
  gives), or the haplotype-based r (D / √(p₁q₁p₂q₂)); either is fine, say which.
- **Orientation**: r is for the `alt` alleles **as spelled in the two variant ids**
  (`chr:pos:ref:alt`, GRCh38). If the server stores a variant in the opposite orientation
  and matches the query by flipping, it must flip the sign of r accordingly. The caller
  harmonises every effect size to the alt allele of the id it passed, so a wrong sign
  here silently reverses an instrument's contribution — this is the one property that
  must be tested, not assumed.
- `r2 == r * r` to floating precision; r in [−1, 1] with at least 6 significant digits;
  `r = 1` for a variant against itself.

Swapping ref and alt in one query id must negate r and leave r² unchanged; that is the
unit test.

## 2. A pairwise matrix endpoint

The MR module needs the full r matrix among a small set of variants in one call. Today a
k-variant matrix costs k neighbourhood queries, each returning every variant in a
1.5 Mb window above an r² threshold, and the pairs below the threshold are lost.

Proposed: `POST /api/ld/matrix` (or `GET` with a comma-separated list) taking

- `variants`: list of `chr:pos:ref:alt` ids, up to a stated maximum (≥ 500 is enough for
  cis-MR and for pruning one gene's candidate leads; state the limit and answer 400 or
  413 naming it when exceeded);
- `panel`: as today.

Returning

- `variants`: the ids **in the caller's spelling and order**, restricted to those the
  panel holds;
- `missing`: the ids the panel does not hold (the request still succeeds for the rest;
  do not 404 the whole call because one variant is absent — a deCODE or UKB-PPP
  instrument absent from a Finnish panel is routine);
- `r`: the k × k matrix (or lower triangle) of signed r **without any r² threshold**,
  with the diagonal 1. A pair on different chromosomes is 0 by definition. A pair the
  server will not compute (same chromosome, further apart than its cap) is `null`, never
  0, so the caller can tell "independent" from "unknown";
- optionally `r2` and `d_prime` matrices, and per-variant `af` (alt-allele frequency in
  the panel), which lets the caller check effect-allele orientation against the
  exposure study's frequency.

If a distance cap applies, state it; cis-MR needs ≤ 2 Mb, pruning ≤ 5 Mb.

**An r² floor of 0.01 is acceptable** (decided 2026-10-09): the matrix may omit pairs with
r² < 0.01 and the caller will treat them as 0. That costs a few percent in the standard
error of a correlated-instrument IVW over the 5–20 instruments cis-MR actually uses, and
the module will repair positive definiteness and state the floor in every result. Two
conditions make it safe: for any two variants the panel holds within the distance cap,
**every** pair at or above the floor must be returned, so absence means "below 0.01" and
never "not computed"; and the floor and the cap are stated in the API docs. The
principal-component estimators over hundreds of variants in a dense region would need
the full matrix, and are out of scope for the first version.

## 3. Unchanged behaviour the suite already depends on

- `GET /api/ld?variant=&window=&panel=&r2_thresh=` keeps its shape (`variation1`,
  `variation2`, `r2`, `d_prime`); results-api forwards only the fields it lists, so new
  fields are additive and nothing breaks if `r` is added.
- Window bounds 100,000–5,000,000 and the 400 text "window must be between …", the 404
  text "variant not found", and the 400 body naming "supported panels: …" are parsed by
  results-api; keep those messages or tell us.
- Panel name `sisu42` stays valid. A non-Finnish European panel (1000G EUR or UKB), if
  one can be served, would matter for deCODE and UKB-PPP instruments, but is not required
  for the first version.

## 4. Variant ids

- `chr:pos:ref:alt`, GRCh38, chromosomes 1–22 and X (say whether X is `X` or `23`, and
  whether `chrN` is accepted); indels spelled with full ref/alt, no padding beyond the
  anchor base; multi-allelic sites distinguished by allele, not position.
- Echo ids back exactly as given so the caller can join on them without re-normalising.

## 5. Validation the implementer can run

1. Pick one gene region (e.g. PCSK9 chr1:55,039,445–55,064,852 ± 500 kb) and compute
   `plink --r --r2` (or the haplotype equivalent) on the panel's genotypes; the API's r
   must match sign and value for every pair.
2. Swap ref/alt in one id and confirm r negates, r² does not.
3. Request a matrix with one id not in the panel and confirm the call succeeds with that
   id in `missing`.
4. Confirm the matrix is symmetric, the diagonal is 1, and no off-diagonal below the old
   r² threshold is missing.

## 6. What the suite has done meanwhile (2026-10-09)

results-api now serves signed r itself from FinnGen R12 **in-sample** LD matrices
(`gs://daly-genetics-results/ld/finngen/r12/`, plink2 `--r-unphased`, 520,210 samples, r and r²
for every pair within 3 Mb at r² ≥ 0.01, both orderings): `GET /ld/{variant}?panel=finngen_r12`
and the pair route `GET /ld/{variant}/{other}?panel=finngen_r12`, with `fields=` selecting any
of r, r², D′ (D′ is not in these files), `GET /ld/panels` stating the floor and cap, and every
response carrying `r2_floor` and `max_distance`. Semantics follow §1–2 as far as the files
allow: r is for the alt alleles as spelled in the ids (plink computes both variants on the
same allele, so the alt-vs-alt sign is right), a swapped ref/alt is 404 naming the panel's
spelling rather than a negated r, a pair absent from the files is "below 0.01" because both
variants' blocks are read, and pairs beyond 3 Mb or on two chromosomes are refused rather than
reported as 0. There is no matrix endpoint: a k-variant matrix is k pair or neighbourhood
calls at ~0.2 s each, which is fine for the 5–20 instruments cis-MR uses and is what the SDK
should wrap first. The upstream's panels (`sisu42`, a reference panel) remain proxied with r²
and D′ only; the sections above still describe what they would need to carry.

The SDK exposes it (same day): `genetics.ld(variant[, other], panel="finngen_r12",
fields=...)` returns `r` beside `r2`, the pair form answers "below the floor" distinctly from
"not in the panel", and `genetics.ld_panels()` says which own panels a deployment serves with
their floor and cap. The MR module will use r for the correlated-instrument estimator, r² for
pruning and proxies, and — since a panel `af` is not in these files — the exposure and outcome
studies' own allele frequencies for the orientation check.
