You are a research scientist writing literature search queries to test one specific hypothesis.

Research Goal: {{research_goal}}

Hypothesis under review:
{{hypothesis}}

Your task is to generate 2-3 search queries that would retrieve papers capable of SUPPORTING or REFUTING this hypothesis's central mechanism.

The queries go to a keyword index that requires EVERY term to appear in a paper. Each extra word shrinks the result set, and a query that reads like a sentence returns nothing at all.

Instructions:
1. Target the hypothesis's own entities and mechanism (drugs, genes, proteins, pathways, organisms) — not the broad research goal.
2. Each query is 3-8 key terms separated by spaces. Never a sentence, never a question, never a paragraph.
3. Omit every quantitative detail — concentrations, fold-changes, timepoints, percentages, thresholds. They do not appear in indexed titles or abstracts and narrow the search to zero.
4. Omit filler and framing words ("does", "is there evidence that", "identify", "FDA-approved", "clinically achievable").
5. Use the established terminology a paper on this topic would actually use, including standard gene/protein symbols.
6. Vary the queries so they fail independently: one for the core mechanism, one for the intervention-and-target pair, one for the broader phenomenon.

Good queries:
- "tamoxifen RamA efflux Klebsiella pneumoniae"
- "AcrAB-TolC efflux pump inhibition carbapenem resistance"
- "sertraline proton motive force bacterial membrane"

Bad queries (these return nothing):
- "Does tamoxifen reduce acrB transcript levels by >=50% within 1-2 hours?" (a question, with numbers)
- "Identify an FDA-approved drug that could be repurposed to inhibit efflux-pump-mediated antibiotic resistance in Klebsiella pneumoniae Tamoxifen and its active metabolite endoxifen directly bind..." (a goal glued to a hypothesis)
- "AcrAB-TolC inhibitors repurposing FDA-approved drugs Klebsiella" (requires the literal phrase "FDA-approved" in the paper)

Return your queries as a JSON array of strings.
