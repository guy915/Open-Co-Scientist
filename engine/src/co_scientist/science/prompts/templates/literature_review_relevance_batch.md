# Judge Paper Relevance to Research Goal

You are judging how well a batch of candidate papers bears on a research goal, before any of them is read in full. Judge each candidate independently -- relevance is not comparative here.

## Research Goal
{{research_goal}}

## Candidates

{{candidates_block}}

---

## Your Task

For **each** candidate, judge only whether its title and abstract bear on the research goal above -- not its citation count, recency, or venue. A paper can be highly relevant and obscure, or well-cited and irrelevant.

Score `relevance` from 0.0 (unrelated to the goal) to 1.0 (directly on point), and give one sentence of `rationale` stating why. Identify each judgment by the candidate's number (`index`) shown above -- do not restate its title or abstract.

## Response Format

Return a JSON object with this structure:

```json
{{
    "judgments": [
        {{"index": 1, "relevance": 0.0, "rationale": "..."}},
        {{"index": 2, "relevance": 0.0, "rationale": "..."}}
    ]
}}
```

Return exactly one judgment per candidate shown above.
