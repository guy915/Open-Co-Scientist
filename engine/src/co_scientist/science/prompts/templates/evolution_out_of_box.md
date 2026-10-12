# Hypothesis Generation Through Out-of-the-Box Thinking

{{domain_context}}

{{domain_evolution_guidance}}

You are an expert researcher tasked with generating a novel, singular hypothesis inspired by analogous elements from provided concepts.

Goal: {{research_goal}}

Instructions:
1. Provide a concise introduction to the relevant scientific domain.
2. Summarize recent findings and pertinent research, highlighting successful approaches.
3. Identify promising avenues for exploration that may yield innovative hypotheses.
4. CORE HYPOTHESIS: Develop a detailed, original, and specific single hypothesis for achieving the stated goal, leveraging analogous principles from the provided ideas. This should not be a mere aggregation of existing methods or entities. Think out-of-the-box.

Criteria for a robust hypothesis:
{{preferences}}

Inspiration may be drawn from the following concepts (utilize analogy and inspiration, not direct replication):

{{partner_context}}

## Run Context

**Original Hypothesis:**
{{original_hypothesis}}

The hypothesis above is this refinement's parent: what you return is recorded as its child and must address the same research goal, but you are not required to preserve the parent's mechanism.

**Review Feedback:**
{{review_feedback}}

**Meta-Review Insights:**
{{meta_review_insights}}

**Specialist Feedback Ledger (debate, ranking, proximity, verification):**
{{specialist_feedback}}

{{supervisor_guidance}}

{{run_guidance}}

{{lab_constraints_section}}

{{falsified_assumptions_section}}

### Literature Review and Analytical Rationale

The following represents an analysis of relevant scientific literature:

{{articles_with_reasoning}}

{{citation_reference_section}}

## Novelty Language

Novelty claims must be hedged unless grounded in retrieved evidence. The literature supplied to this refinement is a bounded retrieval, not the entire current corpus, so the refined proposal must never assert that the idea is the first of its kind, unprecedented, or that no prior work exists. Where the supplied literature or its citation keys establish a gap, cite them; otherwise use hedged phrasing such as "within the retrieved literature", "to our knowledge", or "appears unexplored among the sources examined".

{{diversity_section}}

## Output Format

Work through the instructions above in the order given; the fields below are what you return, and the instructions are how you should arrive at them.

**CRITICAL: Provide ALL NINE components for the resulting hypothesis:**

### 1. Title (required)
A short, authored noun-phrase name for the resulting hypothesis (e.g. "Combinatorial mTOR-Autophagy Rescue"). Never a sentence, never a restatement of the hypothesis text itself, no trailing period.

### 2. \[Technical\] Hypothesis (required)
A dense, testable mechanistic proposition with explicit context and predicted outcome.

### 3. Explanation (required)
Step-by-step layman explanation of the resulting hypothesis (4-6 sentences). Explain mechanisms clearly without oversimplifying.

### 4. \[Practical\] Experiment (required)
A pilot test plan for the resulting hypothesis, as a structured `experiment` object -- not a free-text paragraph: 2-5 ordered `steps` (models, datasets, metrics), ending with the Go/No-Go initial experiment step itself, plus `go_criterion`/`no_go_criterion` giving the exact quantitative pass/fail threshold for that step.

### 5. Refinement Summary (required)
Brief summary naming which supplied concept the analogy came from, what principle was adapted rather than replicated, and how the result departs from the parent.

### 6. Introduction (required)
2-4 sentences of scene-setting background for the problem area this proposal addresses, before any specific mechanism is named. Write it for the hypothesis you are returning, never carried over from the original.

### 7. Recent Findings (required)
2-4 sentences on the recent findings and related research this hypothesis builds on, extends, or departs from. Sets the scene; the grounding below argues this specific hypothesis.

### 8. Literature Grounding (required)
2-4 sentences grounding this hypothesis in the reference list supplied above. Use ONLY the bracketed `[C*]` citation keys given — do NOT invent author-year citations; if no reference list was supplied, state that explicitly. Every sentence here is read as a categorical claim about the intervention this hypothesis names, so it must describe the mechanism you are proposing and no other.

### 9. Safety and Toxicity (required)
2-4 sentences on the safety profile of what this hypothesis proposes: for a pharmacological intervention, known or expected toxicity and the preclinical safety work needed before advancing it; for other domains, the analogous operational or experimental safety considerations. Your own assessment as the proposer, not a review, and about the intervention this hypothesis names.

**Text formatting:**
- Use standard scientific notation and symbols (Greek letters like τ, β, α, mathematical operators like ≥, ≤, ±)
- Do NOT use LaTeX commands (e.g., use 'τ' not '\tau', use '≥' not '\geq')
- Avoid decorative formatting, repeated special characters, or fancy text styling
- Prefer concise plain text when it communicates the idea equally well

Response: a single JSON object carrying all nine components above, and nothing else.
