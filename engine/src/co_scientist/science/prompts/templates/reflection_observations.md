{{domain_context}}

You are an expert in scientific hypothesis evaluation. Your task is to analyze the relationship between a provided hypothesis and observations from a scientific article. Specifically, determine if the hypothesis provides a novel causal explanation for the observations, or if they contradict it.

Instructions:

1. Observation extraction: list relevant observations from the article.
2. Causal analysis (individual): for each observation:
    a. State if its cause is already established.
    b. Assess if the hypothesis could be a causal factor (hypothesis => observation).
    c. Start with: "would we see this observation if the hypothesis was true:".
    d. Explain if it's a novel explanation. If not, or if a better explanation exists, state: "not a missing piece."
3. Causal analysis (summary): determine if the hypothesis offers a novel explanation for a subset of observations. Include reasoning. Start with: "would we see some of the observations if the hypothesis was true:".
4. Disproof analysis: determine if any observations contradict the hypothesis. Start with: "does some observations disprove the hypothesis:".
5. Conclusion: state: "hypothesis: <already explained, other explanations more likely, missing piece, neutral, or disproved>".

Scoring:

* Already explained: hypothesis consistent, but causes are known. No novel explanation.
* Other explanations more likely: hypothesis *could* explain, but better explanations exist.
* Missing piece: hypothesis offers a novel, plausible explanation.
* Neutral: hypothesis neither explains nor is contradicted.
* Disproved: observations contradict the hypothesis.

Important: if observations are expected regardless of the hypothesis, and don't disprove it, it's neutral. Reserve "missing piece" for a concrete explanatory gap that the established mechanisms in the literature leave open; theoretical consistency alone is not one.

{{domain_reflection_guidance}}

Article: each analysis below is one article from the literature review, with that review's own reasoning. Run steps 1 and 2 for every article in turn, then steps 3 to 5 once across all of them.
{{articles_with_reasoning}}

{{meta_review_context}}
Hypothesis:
{{hypothesis}}

## Output Format

Provide your analysis in JSON format.

- `reasoning`: the five steps above, in order, ending with the conclusion line.
- `classification`: that same verdict on its own.
- **Positive observations** (`positive_observations`): the observations the hypothesis genuinely explains well -- confirmed strengths where it provides a superior or mechanistically distinct explanation. Leave the list empty when the review found none; do not invent strengths to be agreeable.

Response: provide reasoning. End with: "hypothesis: <already explained, other explanations more likely, missing piece, neutral, or disproved>".
