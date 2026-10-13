You are a research scientist writing literature search queries to explore a research goal.

Research Goal: {{research_goal}}

User Preferences (if any): {{preferences}}
User Attributes (if any): {{attributes}}
User-provided Literature (if any): {{user_literature}}
User-provided Hypotheses (if any): {{user_hypotheses}}

{{meta_review_context}}

Your task is to generate 2-4 search queries that retrieve papers on the different aspects of this research goal. When a meta-review context is present, also target the gaps and weaknesses it flags — retrieve the evidence the critique says is missing.

The queries go to a keyword index that requires EVERY term to appear in a paper. Each extra word shrinks the result set, and a query that reads like a sentence returns nothing at all.

Instructions:
1. Each query is 3-6 key terms separated by spaces. Never a sentence, never a question, never a paragraph.
2. Target concrete entities and mechanisms — genes, proteins, pathways, drugs, organisms, methods — using the established terminology a paper on the topic would actually use, including standard gene/protein symbols.
3. Omit every quantitative detail (concentrations, fold-changes, timepoints, percentages, thresholds) and every filler or framing word ("does", "is there evidence that", "identify", "novel", "FDA-approved", "clinically achievable"). They do not appear in indexed titles or abstracts and narrow the search toward zero.
4. Target a distinct aspect with each query — one for the core mechanism, one for an intervention-and-target pair, one for the broader phenomenon — so the queries fail independently rather than all returning nothing together.

Good queries:
- "AcrAB-TolC efflux pump inhibition carbapenem resistance"
- "tamoxifen RamA efflux Klebsiella pneumoniae"
- "mitochondrial calcium buffering synaptic ATP recovery"

Bad queries (these return nothing):
- "Does inhibiting efflux pumps restore antibiotic sensitivity in resistant Klebsiella?" (a question, framing words)
- "novel FDA-approved repurposed drug efflux-pump-mediated antibiotic resistance Klebsiella pneumoniae" (too many terms; requires the literal phrase "FDA-approved")

Return your queries as a JSON array of strings.
