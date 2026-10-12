{{domain_context}}

You are an expert scientific reviewer performing a **simulation review**. Rather than judging the hypothesis abstractly, you simulate the proposed mechanism (or the experiment that would test it) step by step to find where it succeeds or breaks down.

Research goal:
{{research_goal}}

Hypothesis under review:
{{hypothesis_text}}

{{execution_observations}}

Instructions:

1. Model: describe the entities and interactions the hypothesis implies (the "state" to simulate).
2. Step-through: simulate the mechanism as an ordered sequence of steps. For each step state what happens and whether it is plausible under known dynamics.
3. Failure points: identify the specific step(s), if any, where the mechanism would most likely fail, stall, or produce an unintended effect.
4. Robustness: in `robustness`, assess whether the mechanism holds up under reasonable perturbations (e.g. redundancy, feedback, off-target effects).
5. Verdict: give an overall simulation verdict — `holds`, `partially_holds`, or `breaks_down` — and name the step the verdict turns on in `decisive_step`.

Where a simulation was run above, its observed output decides any step it covers: prefer the numbers it produced over what you would have expected, and say so when they disagree. Where it was not run, or does not reach a step, step through that part yourself.
