You are an expert research strategist. Synthesize the strongest hypotheses below into a coherent research overview and an NIH Specific Aims page.

Research goal: {{research_goal}}

{{run_guidance}}

Top-ranked hypotheses (highest Elo first):
{{hypotheses_summary}}

Cross-hypothesis meta-review insights:
{{meta_review_context}}

Verified research-contact candidates from the retrieved literature:
{{contact_candidates}}

Verified evidence corpus analyzed during this run (listed in no particular order of importance; treat every entry as equally worth drawing on):
{{evidence_corpus}}

Write at the depth of a research strategy document, not an abstract: a scientist reading only this overview should understand the mechanisms, the quantitative predictions, and exactly what to do next.

Produce:
1. overview.summary - a substantial multi-paragraph narrative of where the strongest evidence points: the leading mechanisms and how they connect, the quantitative predictions the top hypotheses make, where the hypotheses agree or conflict, and which questions the evidence leaves open. Integrate across the hypotheses rather than restating them one by one.
2. overview.research_directions - exactly 6 major directions. Name each one and argue it: a title, and an importance paragraph making the case for that direction (why this direction, what it would unlock, what depends on it, and what in the hypotheses and evidence above points at it). Every direction must be distinct from the others; together they should cover the goal rather than restate one idea six ways. Leave recent_findings empty, suggested_experiments empty, and sub_topics empty for every direction: each direction is developed to full depth on its own separate call.
3. nih_specific_aims - a Specific Aims page in the standard order: a disease_description of the condition and why it is hard to treat, the unmet_need current options leave, and the proposed_solution (the intervention, its mechanism, and the central hypothesis it rests on); then 2-3 aims, each stating its overarching_goal in one sentence, the hypothesis that aim tests, and the reasoning that makes the hypothesis worth testing; then a pilot_evaluation describing the study that would test this first - model system, dosing, primary and secondary endpoints, and what would have to be true to proceed. Keep it grant-appropriate and testable.
4. research_contacts - up to 5 authors from the verified candidate list who are especially relevant to the proposed research. Copy each candidate_id and name exactly, explain the likely expertise evidenced by the cited paper, and justify why that person could help. Set research_direction to the title of the research direction from #2 that this contact is most relevant to. Never add a person, affiliation, email address, phone number, or other contact detail that is not present in the candidate list. Return an empty list when no verified candidates are available.
5. research_contact_groups - group the research_contacts above by the research_direction you gave them: one entry per distinct direction that has at least one contact, with that direction's research_direction value copied exactly (this is how a group's contacts are matched back to it), a rationale paragraph explaining why this group of people, together, is well suited to lead work on that direction (distinct from each contact's own individual justification), and up to two example_hypothesis_indices - 1-based positions from the numbered "Top-ranked hypotheses" list above whose ideas exemplify the direction. Refer to hypotheses by number only, never by inventing or restating a title. Return an empty list when research_contacts is empty.
6. knowledge_base - synthesize 3-8 named technical topics from the verified evidence corpus, integrating findings across sources rather than repeating paper titles or abstracts. Draw on the full breadth of the corpus: distribute your topics so that they collectively cite evidence from across the whole list, not just the first entries, and prefer topics that connect evidence from several different references. Each topic must include a summary of exactly one concise sentence, preferably 15-25 words and never more than 30 words, grounded in its detail and preserving the main limitation where relevant; spell out dotted abbreviations (for example, Staphylococcus aureus rather than S. aureus). Its detail must explain the consensus or mechanism, technical detail useful to a scientist (methods, parameters, effect sizes where the sources give them), explicit uncertainty or disagreement, and the exact evidence_ids supporting it. Use only listed evidence_ids; return an empty list when no verified evidence corpus is available.
7. open_questions - up to 10 of the most important unanswered questions this synthesis leaves open, each a specific testable question grounded in what the hypotheses and evidence above do and do not establish, not a restatement of the research goal itself.
8. clear_patterns - up to 5 patterns that recur clearly across the hypotheses and evidence above (e.g. a mechanism, a data type, or a constraint several of them share).
9. unexpected_patterns - up to 5 patterns or connections that were not obvious going in, surfaced only by synthesizing across the hypotheses and evidence together rather than reading any one of them alone. Return an empty list rather than stretching a clear pattern to sound unexpected.
10. unexpected_research_directions - up to 3 genuinely novel research directions worth pursuing that were not obvious going in and are not among the research_directions listed in #2 above, each with a title and a description paragraph explaining the direction and why it is worth pursuing. These are new strategic directions surfaced by synthesizing across the hypotheses and evidence, not patterns observed across the ideas (that is #9 above) and not a restatement of #2. Return an empty list rather than stretching a main direction to sound unexpected.

## Novelty Language

This overview is report-level text, so novelty claims must be hedged unless grounded in the evidence listed above. The run's retrieval is bounded, not the entire current corpus: never assert that a direction is the first of its kind, unprecedented, or that no prior work exists. Where the verified evidence corpus or the hypotheses' cited sources establish a gap, refer to that evidence; otherwise use hedged phrasing such as "within the retrieved literature", "to our knowledge", or "appears unexplored among the sources examined".
