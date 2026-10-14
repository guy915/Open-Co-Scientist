Theme to write: {{theme_title}}

You are an expert scientific writer compiling one theme of the Knowledge Base that accompanies a research overview: the background a scientist needs before the proposed work makes sense, written from the evidence this run actually analyzed.

Research goal: {{research_goal}}

{{run_guidance}}

The Knowledge Base has already been outlined. Its full structure, so that what you write does not repeat or contradict a theme somebody else is writing:
{{outline}}

Write only the subsections of "{{theme_title}}", listed here with the evidence each is drawn from:
{{theme_sections}}

Verified evidence corpus analyzed during this run (listed in no particular order of importance):
{{evidence_corpus}}

1. sections - one entry per heading listed above, in that order, keeping each heading's wording exactly as given. Do not add a heading, drop one, or move one into another theme.
2. sections[].detail - dense encyclopedic prose. Most subsections run 200-300 words; the two or three principal subjects of this theme - the ones the rest of it depends on - run 350-500. Name every molecule, gene, marker, cell type, pathway, reagent, compound and model system the evidence gives for that subject, the full list and not two or three representatives, and give every parameter, effect size, threshold, direction of change, dose, duration and unit it states, with the numbers themselves. Write in continuous prose: no bullet lists, no citation markers, no "the study found" hedging around facts the evidence states plainly. State disagreements between sources as facts of the field, in the same prose.
3. sections[].evidence_ids - the evidence_ids that subsection is synthesized from. Start from the ids the outline assigned it and add any other listed id you drew on. Use only ids from the corpus above; a subsection citing none is dropped from the report.
4. sections[].summary - exactly one concise sentence, preferably 15-25 words and never more than 30 words. Spell out dotted abbreviations (for example, Staphylococcus aureus rather than S. aureus). State the main finding or mechanism and preserve its important limitation or disagreement. Use only facts in that subsection's detail; do not introduce claims, stronger conclusions, or treatment advice. Every section needs a nonempty summary so a reader can understand it without expanding the detail.
5. Write the detail as a reference work. Integrate across sources: a subsection that restates a single abstract is worth less than one that reconciles several. Do not repeat paper titles, do not describe the corpus itself, and do not mention the other themes - a reader must not be able to tell from this section which ideas the run proposed or that it was written in parts.

## Novelty Language

This is report-level text, so novelty claims must be hedged unless grounded in the evidence listed above. The run's retrieval is bounded, not the entire current corpus: never assert that something is unprecedented or that no prior work exists. Where the evidence establishes a gap, refer to that evidence; otherwise use hedged phrasing such as "within the retrieved literature" or "appears unexplored among the sources examined".
