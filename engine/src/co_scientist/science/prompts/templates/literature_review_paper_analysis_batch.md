# Analyze Research Papers for Hypothesis Generation Opportunities

You are analyzing several research papers to identify opportunities for novel hypothesis generation. Analyze each paper on its own evidence; do not merge papers or carry a finding from one paper into another's analysis.

## Research Goal
{{research_goal}}

## Papers
{{papers}}

---

## Your Task

For every paper above, extract:

1. `key_findings`: main contributions and results. What did they discover or demonstrate?
2. `gaps_identified`: limitations or gaps the authors explicitly mention; questions that remain unanswered.
3. `future_work`: research the authors suggest; next steps they propose.
4. `methodology_limitations`: constraints in their methods; what they could not test or measure.
5. `unexplored_areas`: topics, variables or approaches mentioned but not investigated.
6. `relevance`: how the paper relates to the research goal and what context it provides.

Focus on actionable insights that could inform novel hypotheses. Be specific and cite details from each paper.

## Response Format

Return a JSON object with one entry in `analyses` per paper, each carrying the paper's number as `paper_index`:

```json
{{
    "analyses": [
        {{
            "paper_index": 1,
            "key_findings": "...",
            "gaps_identified": "...",
            "future_work": "...",
            "methodology_limitations": "...",
            "unexplored_areas": "...",
            "relevance": "..."
        }}
    ]
}}
```
