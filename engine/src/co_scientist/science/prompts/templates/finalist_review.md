{{domain_context}}

You are an expert scientific reviewer. This hypothesis is a finalist of the tournament, so it gets one in-depth review in four parts, answered together in one JSON object: a literature observation analysis, a full review, a simulation review, and the search queries that will verify it. Draw on your knowledge of the field and on the evidence provided. Do not invent citations.

Research goal:
{{research_goal}}

Hypothesis under review:
{{hypothesis_text}}

## Part 1: `observation` (literature observations)

{{articles_with_reasoning}}

Analyze whether the hypothesis gives a novel causal explanation for the observations in the articles above, or whether they contradict it. In `reasoning`, concisely:
1. List the relevant observations from the articles.
2. For each: is its cause already established; would we see it if the hypothesis were true; is the hypothesis a novel explanation, or does a better one exist ("not a missing piece")?
3. Say whether the hypothesis newly explains a subset of the observations.
4. Say whether any observation contradicts the hypothesis.
5. End with "hypothesis: <already explained, other explanations more likely, missing piece, neutral, or disproved>".

`classification` repeats that verdict on its own:
- Already explained: consistent, but the causes are known.
- Other explanations more likely: it could explain them, but better explanations exist.
- Missing piece: a novel, plausible explanation for a concrete gap that established mechanisms leave open. Theoretical consistency alone is not one.
- Neutral: neither explains nor is contradicted. Observations expected regardless of the hypothesis are neutral.
- Disproved: observations contradict it.

In `positive_observations`, list the observations the hypothesis genuinely explains well. Leave the list empty when there are none; do not invent strengths.

{{domain_reflection_guidance}}

## Part 2: `full_review`

1. `correctness`: whether the hypothesis and its mechanism are scientifically correct and internally consistent. Name any logical or factual errors.
2. `assumptions`: the key assumptions it depends on. For each, first `reasoning` (2-4 sentences on why the evidence does or does not back it, citing specific evidence where available), then `support` of exactly `supported`, `uncertain` or `likely_false`. An assumption nothing supports is `uncertain`.
3. `quality_and_novelty`: the rigor of the formulation, and whether it is a genuine, non-obvious contribution relative to established work.
4. `literature_grounding`: known results that support or undermine it, or say that none are available.
5. `verdict`: `sound`, `needs_revision` or `rejected`, with a concise `justification`.
6. `go_no_go_recommendation`: a short advisory testing recommendation (e.g. "Go — pursue wet-lab validation"). Use an empty string if it adds nothing beyond `verdict`.
7. `time_to_verdict`: a brief timeframe to a decisive experimental result (e.g. "2-4 weeks"). Use an empty string if you cannot estimate one.
8. `comparison_with_knowledge_base`: what established knowledge it agrees with, and what it contradicts.
9. `goal_requirements_assessment`: the hypothesis against each requirement the research goal states, naming any it does not meet.
10. `feasibility_steps`: the concrete steps that would test it, in order; then `feasibility_reasoning`: why they are or are not practical (resources, techniques, time).
11. `impact_assessment`: what changes in the field if it holds, and by how much.
12. `reviews_summary`, in eight parts. `executive_verdict` is a paragraph stating what the hypothesis proposes and whether it stands, ending in an explicit verdict. `critical_flaws`, `addressed_objections`, `validated_risks`, `supporting_arguments`, `alignment_and_novelty` and `feasibility_assessment` are short lists, one entry per point. `conclusion` is a closing paragraph: what the hypothesis is worth, and what would have to change for it to be worth testing. Leave a list empty when you have nothing for it, but answer every part.

## Part 3: `simulation`

Simulate the proposed mechanism, or the experiment that would test it, step by step to find where it succeeds or breaks down.

{{execution_observations}}

1. `model`: the entities and interactions the hypothesis implies.
2. `steps`: the mechanism as an ordered sequence; for each step, what happens and whether it is `plausible` under known dynamics.
3. `failure_points`: the step(s) where it would most likely fail, stall or produce an unintended effect.
4. `robustness`: whether it holds up under reasonable perturbations (redundancy, feedback, off-target effects).
5. `verdict`: `holds`, `partially_holds` or `breaks_down`, and the step it turns on in `decisive_step`.

Where a simulation was run above, its observed output decides any step it covers: prefer its numbers over your expectations, and say so when they disagree.

## Part 4: `verification_queries`

Up to 4 search queries for the papers that would confirm or refute the assumptions you found least certain. Each goes to a keyword index that requires every term to appear: use 3-8 established terms, standard gene/protein/drug symbols, no question form, filler words or numbers.
- "Does tamoxifen reduce acrB transcript levels by >=50% within 1-2 hours in K. pneumoniae?" -> "tamoxifen acrB expression Klebsiella pneumoniae"

{{tool_instructions}}
