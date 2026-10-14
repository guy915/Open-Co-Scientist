# Hypothesis Feasibility Improvement

{{domain_context}}

{{domain_evolution_guidance}}

You are an expert in scientific research and technological feasibility analysis. Your task is to refine the provided conceptual idea, enhancing its practical implementability by leveraging contemporary technological capabilities. Ensure the revised concept retains its novelty, logical coherence, and specific articulation.

Goal: {{research_goal}}

Guidelines:
1. Begin with an introductory overview of the relevant scientific domain.
2. Provide a concise synopsis of recent pertinent research findings and related investigations, highlighting successful methodologies and established precedents.
3. Articulate a reasoned argument for how current technological advancements can facilitate the realization of the proposed concept.
4. CORE CONTRIBUTION: Develop a detailed, innovative, and technologically viable alternative to achieve the objective, emphasizing simplicity and practicality.

Evaluation Criteria:
{{preferences}}

Original Conceptualization:
{{original_hypothesis}}

## Run Context

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

Work through the guidelines above in the order given; the fields below are what you return, and the guidelines are how you should arrive at them.

**CRITICAL: Provide ALL NINE components for the refined hypothesis:**

### 1. Title (required)
A short, authored noun-phrase name for the refined hypothesis (e.g. "Combinatorial mTOR-Autophagy Rescue") -- update it to reflect what changed, or keep it if the core idea did not. Never a sentence, never a restatement of the hypothesis text itself, no trailing period.

### 2. \[Technical\] Hypothesis (required)
A dense, testable mechanistic proposition with explicit context and predicted outcome.

### 3. Explanation (required)
Updated step-by-step layman explanation reflecting any refinements made (4-6 sentences). Explain mechanisms clearly without oversimplifying.

### 4. \[Practical\] Experiment (required)
An updated pilot test plan for the refined hypothesis, as a structured `experiment` object -- not a free-text paragraph: 2-5 ordered `steps` (models, datasets, metrics), ending with the Go/No-Go initial experiment step itself, plus `go_criterion`/`no_go_criterion` giving the exact quantitative pass/fail threshold for that step.

### 5. Refinement Summary (required)
Brief summary explaining what the feasibility improvement changed and why, including what current technological capability makes the revised concept implementable.

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
