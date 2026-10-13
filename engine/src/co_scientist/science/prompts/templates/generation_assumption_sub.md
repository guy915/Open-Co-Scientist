{{domain_context}}

You are an expert scientist performing the SECOND LEVEL of an iterative assumption analysis: decomposing load-bearing assumptions into the finer-grained sub-assumptions they rest on.

Research goal:
{{research_goal}}

The numbered parent assumptions below were identified as load-bearing in the first level. Decompose EACH of them into its constituent sub-assumptions: the more specific claims that must all hold for the parent to hold.

{{parents_list}}

{{literature_context}}
{{citation_reference_section}}
Instructions:

1. For every numbered parent above, emit one entry whose `parent_index` is that parent's 0-based index from the list (identify parents by index only; never repeat their text).
2. List up to {{max_sub_per_parent}} sub-assumptions per parent — the distinct, more specific claims the parent decomposes into.
3. Prefer sub-assumptions the evidence above leaves unexamined; those are the ones worth challenging later.

Keep each sub-assumption to one precise sentence. Do not generate hypotheses yet; a later step does that from this tree.
