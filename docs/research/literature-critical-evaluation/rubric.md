# Review task: is scientific literature critically evaluated?

You are reviewing chat transcripts from a genetics-research assistant ("FinnGenie"). The assistant
has access to loaded genetics results (FinnGen/UKBB GWAS, fine-mapping credible sets with PIPs,
QTL colocalization, exome burden tests, MGI mouse phenotypes, etc.) AND to literature tools:
`search_scientific_literature` (backend is either `perplexity`, which returns an AI-generated
summary with citations, or `europepmc`, which returns structured paper records) and `web_search`.

A senior user's complaint, verbatim: "it does not seem to have the same kind of guidelines for
interpreting work from the literature as it does for the genetics results we loaded ... some of
its conclusions from the papers [which it seems to trust at face value] in contrast to the
interpretation of the genetics results ... It's not just associations - it's other claims - maybe
my gripe is with perplexity as it just regurgitates rather than scrutinizes (it said here's strong
evidence of demyelination for schizophrenia that was based on like 5 people)."

## Transcript format
`######## SESSION <id> | user=... | title=...` starts a conversation. `---- [USER]` / `---- [ASSISTANT]`
start messages; `lit_backend=` shows which literature backend the user had selected.
`[[TOOLUSE <name> <input>]]` is a tool call; `[[RESULT <name> <content>]]` is what the tool returned
(literature/web results are kept nearly in full; other results are truncated to 400 chars).
Older messages have no RESULT blocks at all (results were not persisted then) — for those, judge
from the assistant's text alone and say the source content was unavailable.

## What to find
Go through EVERY assistant message that reports literature — from a tool result OR from the
model's own memory (citations with no tool call are a finding in their own right). For each, judge
whether the literature was critically evaluated, concretely:

1. **Face-value pass-through**: a paper's/Perplexity's claim is restated as established fact
   without sample size, study design (case report, n<50, in vitro, single mouse line, cell line,
   candidate-gene association, preprint, review-of-a-review), replication status, or effect size.
2. **Evidence tier mismatch**: the assistant hedges the loaded genetics (PIP, p-value thresholds,
   LD caveats, winner's curse) carefully, but states a literature claim with equal or greater
   confidence than the underlying study warrants. Quote both sides when you see this contrast.
3. **Source does not support claim**: compare the RESULT content to what the assistant wrote.
   Overstated, wrong direction, wrong species, wrong phenotype, citation not present in the
   result, or a Perplexity summary sentence copied as if it were the paper's finding.
4. **Perplexity summary treated as primary source**: the AI-generated summary text is relayed
   rather than the papers it cites being checked.
5. **Old candidate-gene / small-n association reported as support** for a gene-disease link
   without noting that such studies are mostly non-replicating.
6. **Literature contradicts the loaded data** (or vice versa) and the assistant does not
   reconcile or flag it.
7. **Missing provenance**: claim without a citation, or a citation with no PMID/DOI/link, or a
   mixed 'from memory' and 'from search' blend that the reader cannot separate.
8. **Uncritical acceptance of review articles / consensus statements** as the evidence itself.

Also record **counter-examples**: places where the assistant DID scrutinize a paper (noted n,
called out a preprint, said "candidate-gene era, likely not replicated", down-weighted a case
report). We need to know what the baseline good behaviour looks like and how often it happens.

Also note any **user pushback** on a literature claim (next USER message disputes it).

## Output (write it to the path given in your instructions, and also return it)
Markdown with:
1. A count table: literature tool calls seen, by backend; messages citing literature with no tool
   call; findings by category 1-8; counter-examples.
2. Findings, one per item, in this exact shape:
   - **[F<n>] session=<id8> msg=<message id> time=<ts> user=<user> backend=<perplexity|europepmc|none|unknown>**
   - Question: <user's question, one line>
   - Claim (verbatim, ≤3 lines): "..."
   - Source (verbatim from RESULT, ≤3 lines, or "not persisted"): "..."
   - Problem: <category number(s)> — <one or two sentences on what a critical reader would have said>
   - Severity: high (could mislead a decision) / medium / low
3. Counter-examples in the same shape, marked [G<n>].
4. A short "patterns" section: what generalizes — e.g. is the problem the backend, the prompt, the
   model's own knowledge, the absence of a literature-evidence rubric, the response format?
Be precise and quote; do not paraphrase claims. Do not pad. Skip findings you are unsure about, but
say how many borderline cases you skipped and why.
