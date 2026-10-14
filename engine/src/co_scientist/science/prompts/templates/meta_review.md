{{domain_context}}

{{domain_review_guidance}}

You are an expert in scientific research and meta-analysis. Synthesize a comprehensive meta-review of provided reviews pertaining to the following research goal:

Goal:
{{research_goal}}

Preferences:
{{preferences}}

Additional instructions:
{{instructions}}

**Hypotheses Process Supervisor Guidance**
{{supervisor_guidance}}

{{run_guidance}}

Provided reviews for meta-analysis:
{{all_reviews}}

Instructions:

* Generate a structured meta-analysis report of the provided reviews.
    - `meta_review_summary`: the synthesis as a whole -- how the reviewed set aligns with the research goal, its potential for scientific impact, and the most promising avenues for further exploration.
    - `process_assessment`: how this run's own generation, review and evolution stages performed -- where generation could improve, gaps in the review criteria or approach, and the consistency and quality of the reviews.
    - Where present, each review history's ``mature_reviews`` block carries the full, simulation, and recurrent review findings for that hypothesis; treat their verdicts and failure points as reviewer feedback alongside the initial reviews.
* Focus on identifying recurring critique points and common issues raised by reviewers.
    - `recurring_themes`: the critique points and issues that recur across the reviews, organized as a taxonomy rather than a flat list. Give a handful of broad themes (aim for about five, e.g. "Core Hypothesis and Mechanism", "Experimental Design and Feasibility", "Novelty and Impact", "Assumptions and Validation", "General Advice"), each with a short `description` of what it covers and a `frequency` saying how often it recurs in the reviews' own terms.
        - `recurring_themes[].sub_themes`: under each theme, the named critique points that recur within it (e.g. "Primary Driver vs. Consequence", "Model System Limitations"), each with its own short name as `theme` and a `description` stating what reviewers said and how widely — narrate the prevalence in that prose ("a very common critique", "several ideas were criticized"), not as a count.
        - `recurring_themes[].sub_themes[].points`: where a critique point breaks down further, the concrete guidance a future proposal should follow to answer it, one short sentence each. Leave empty for a point that needs no breakdown.
    - `strengths` and `weaknesses`: what the reviews find in common across the hypotheses.
    - `potential_connections`: relationships between hypotheses, cross-cutting themes, and -- ONLY when truly beneficial -- opportunities to synthesize complementary ideas. Avoid recommending a synthesis that would make hypotheses too similar or identical; preserve distinct methodologies and biomarker types across hypotheses.
* The generated meta-analysis should provide actionable insights for researchers developing future proposals.
    - `strategic_recommendations`: high-level directions for improving hypothesis quality, the areas the evolution agent should focus on, and new directions or perspectives worth exploring. Give DISTINCT recommendations rather than the same generic advice, so diversity is preserved -- several different approaches are better than converging on one "best" solution.
    - Where a `strategic_recommendations` entry is one step of a staged roadmap, set its `time_estimate` to that step's timeline (e.g. "Weeks 1-2", "Month 3+") and, when the roadmap splits a step into parts, its `phase_label` (e.g. "Phase A"). Leave both empty for a step with no explicit timeline or sub-phase. Where a step selects one reviewed idea to proceed with, name it in `recommended_idea` using the `hypothesis_index` convention described under Output Format -- never restate the idea's own text.
    - `main_research_directions`: exactly two flowing prose paragraphs, separated by a blank line -- never a bulleted list, and never a restatement of `strategic_recommendations`. Weave the run's main directions together into connected narrative: name each direction inline in **bold** the first time it appears, explain briefly why it matters, and close the second paragraph with an unanticipated observation that cuts across more than one direction (e.g. "Unexpectedly, recent synthesis suggests ..."). This mirrors the published report's own "Main Research Directions" section -- a strategic-landscape narrative, not an itemized list.
* Refrain from evaluating individual proposals or reviews; focus on producing a synthesized meta-analysis.
    - The two comparison tables below belong to that synthesis rather than being an exception to it: they position the reviewed pool as a set on shared axes and carry no verdict on any single proposal, so name an idea only to place it on the axes, never to review it.
    - Choose your own comparison axes -- do not default to a fixed, generic vocabulary (e.g. "computational scalability", "implementation complexity") that may not fit this research goal's own discipline. A chemical-biology goal might rate ideas on off-target risk and synthetic accessibility; a clinical-epidemiology goal on cohort availability and confounding risk; a wet-lab mechanism question on which perturbation and readout it needs. Pick 2-5 axes that a reader of *this* goal would actually want to know, name them plainly, and rate every idea/row on the same axes so the table stays comparable across rows.
    - `candidate_comparison.thematic_summary`: how the candidate hypotheses group into mechanistic themes, and which is best supported by the evidence reviewed above
    - `candidate_comparison.axes`: 2-5 short axis names fitting this goal's own discipline, as described above
    - `candidate_comparison.ideas`: one entry per hypothesis worth distinguishing (not necessarily every one), each naming its `idea` by `hypothesis_index` and subject (e.g. "Hypothesis 3: LILRB4 blockade" -- never the full hypothesis text), plus `values`: one rating per entry in `candidate_comparison.axes`, in the same order
    - `existing_solutions_comparison.summary`: how current standard-of-care approaches for this research goal compare to the candidate hypotheses as a group. Leave this and `rows`/`axes` empty when the goal has no established standard-of-care or existing-solutions landscape to compare against (e.g. a basic mechanism question) -- do not invent one
    - `existing_solutions_comparison.axes`: 2-5 short axis names for comparing each existing approach against the candidate ideas, following the same discipline-fit guidance as `candidate_comparison.axes`
    - `existing_solutions_comparison.rows`: one entry per named existing method or standard practice, each with its `method` and `values`: one rating per entry in `existing_solutions_comparison.axes`, in the same order

## Output Format

Provide your meta-review analysis in JSON format.

**Text formatting guidelines:**
- Use standard scientific notation and symbols (Greek letters like τ, β, α, mathematical operators like ≥, ≤, ±)
- Do NOT use LaTeX commands (e.g., use 'τ' not '\tau', use '≥' not '\geq')
- Avoid decorative formatting, repeated special characters, or fancy text styling
- Prefer concise plain text when it communicates the idea equally well
- When you refer to a specific hypothesis, use its `hypothesis_index` verbatim (these are numbered from 1) and name its subject, e.g. "Fluspirilene (Hypothesis 1)". Never renumber them and never count from 0 — a scientist reads these labels.

Response:
