{{domain_context}}

You are an expert tasked with formulating a novel and robust hypothesis to address the following objective.
Describe the proposed hypothesis in detail, including specific entities, mechanisms, and anticipated outcomes.
This description is intended for an audience of domain experts.
You have conducted a thorough review of relevant literature and developed a logical framework for addressing the objective. The articles consulted, along with your analytical reasoning, are provided below.

Goal: {{goal}}

Criteria for a strong hypothesis:
{{preferences}}

Attributes to prioritize:
{{attributes}}

Existing hypothesis (if applicable):
{{user_hypotheses}}

{{instructions}}

{{supervisor_guidance}}
{{meta_review_context}}
{{run_guidance}}
{{domain_generation_guidance}}

{{lab_constraints_section}}

{{research_expansion_section}}

{{falsified_assumptions_section}}

Literature review and analytical rationale (chronologically ordered, beginning with the most recent analysis):

#BEGIN LITERATURE REVIEW#
```
{{articles_with_reasoning}}
```
{{articles_metadata}}

#END LITERATURE REVIEW#

{{citation_reference_section}}

## Available Tools

The literature review above is context for the research landscape. Search for the specific papers behind each hypothesis with the tools below, read what you retrieve, and cite the gap you identify with the `[C*]` keys from the Citation Reference List.

{{tool_instructions}}

## Novelty Language

Novelty claims must be hedged unless grounded in retrieved evidence. The literature available to this run is a bounded retrieval, not the entire current corpus, so never assert that an idea is the first of its kind, unprecedented, or that no prior work exists. When the retrieved evidence establishes a gap, cite the relevant `[C*]` keys; otherwise use hedged phrasing such as "within the retrieved literature", "to our knowledge", or "appears unexplored among the sources examined".

Proposed hypothesis (detailed description for domain experts):

Write {{hypotheses_count}} of them, each addressing a different gap or approach. Draft each idea to full depth already: the validation phase preserves what you write, so a shallow draft becomes a shallow final hypothesis. Name concrete entities, mechanisms, and directions of effect; give quantitative predictions where the domain allows; and specify a complete experiment (model system, groups and controls, quantitative readouts, support/falsification criteria).

**CRITICAL**: After using tools to examine papers, respond with ONLY the raw JSON object. Do NOT wrap it in markdown code blocks (no ``` or ```json). Start your response directly with { and end with }.

**Output JSON structure:**

```json
{
  "drafts": [
    {
      "hypothesis": "Precise mechanistic proposition with context, intervention or observation, and predicted outcome",
      "explanation": "Step-by-step layman explanation (4-6 sentences)",
      "gap_reasoning": "Brief explanation of what gap in the literature this hypothesis addresses and why it seems promising",
      "literature_sources": "Bracketed citation keys from the reference list that informed this gap. Example: 'Gap identified via retinal imaging [P1] and mechanistic data [KG1].'",
      "experiment": "Concrete experiment design with models, datasets, metrics, and validation criteria (4-6 sentences)"
    }
  ]
}
```

**Field requirements:**
- `hypothesis`: Technical, falsifiable formulation; do not force a fixed sentence template
- `explanation`: Clear explanation for technical audiences in layman terms
- `gap_reasoning`: What research gap this addresses and why it's promising
- `literature_sources`: **CRITICAL - Use ONLY `[C*]` keys from the Citation Reference List (if provided). Do NOT invent author-year citations.**
- `experiment`: Concrete, actionable experiment design to test the hypothesis

**Text formatting guidelines:**
- Use standard scientific notation and symbols (Greek letters like τ, β, α, mathematical operators like ≥, ≤, ±)
- Do NOT use LaTeX commands (e.g., use 'τ' not '\tau', use '≥' not '\geq')
- Avoid decorative formatting, repeated special characters, or fancy text styling
- If copying from literature, convert LaTeX notation to Unicode symbols or plain text
- Prefer concise plain text when it communicates the idea equally well

Output raw JSON with a "drafts" array of {{hypotheses_count}} objects carrying the 5 required fields above.
