{{domain_context}}

You are an expert scientist performing the FIRST LEVEL of an iterative assumption analysis: decomposing a research area into the assumptions its current thinking takes for granted. Later levels decompose the load-bearing assumptions further, and hypotheses are generated from the resulting tree.

Research goal:
{{research_goal}}

{{meta_review_context}}
{{literature_context}}
{{citation_reference_section}}
{{research_expansion_section}}
{{falsified_assumptions_section}}
Instructions:

1. List up to {{max_top_assumptions}} key assumptions currently taken for granted in this research area. Cover different kinds of assumptions: mechanistic (how the system works), methodological (how it is studied), and conceptual (how the problem is framed).
2. For each assumption, set `load_bearing` to true when much of the area's reasoning depends on it, so that challenging it would open genuinely new hypothesis space. Set it false for peripheral or already-contested assumptions.
3. Ground your assessment in the literature context above when one is provided: prefer assumptions the evidence leaves unexamined or only weakly supported.

Keep each assumption to one or two precise sentences. Do not generate hypotheses yet; a later step does that from this tree.
