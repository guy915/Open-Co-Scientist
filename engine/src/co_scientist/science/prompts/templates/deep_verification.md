You are an expert scientific reviewer performing a DEEP VERIFICATION of a hypothesis via probing questions.

Research goal: {{research_goal}}

Hypothesis under verification:
{{hypothesis_text}}

Retrieved evidence available for verification:
{{evidence_context}}

Instructions:
1. Decompose the hypothesis into its fundamental, load-bearing assumptions.
2. For each major assumption, write a probing QUESTION that challenges whether it actually holds (prefer the assumptions whose failure would most undermine the hypothesis).
3. For each question, give the best-faith ANSWER grounded in the retrieved evidence when available, distinguish direct support from inference or absence, then a REASONING paragraph judging how well the assumption survives, and set assumption_is_fundamental to true if a failure would invalidate the core claim.
4. For each question, also write a SEARCH_QUERY: the 3-8 key terms a paper answering that question would actually contain. This goes to a keyword index that requires every term to appear, so the question itself would match nothing. Drop the question form, all filler words, and every number (concentrations, fold-changes, timepoints, percentages) — use only established terminology and standard gene/protein/drug symbols.
   - Question: "Does tamoxifen reduce acrB transcript levels by >=50% within 1-2 hours in K. pneumoniae?" -> search_query: "tamoxifen acrB expression Klebsiella pneumoniae"
   - Question: "Is there evidence that sertraline dissipates proton motive force in bacteria?" -> search_query: "sertraline proton motive force bacterial membrane"
5. Sub-assumption decomposition: break the hypothesis into at most 5 sub-assumptions (the finer claims that must all hold for it to hold) and verify each one against the retrieved evidence. For each, record the assumption itself, a verification that says what the evidence shows for or against it (distinguishing direct support from inference or absence), and a status: "supported" (the evidence backs it), "uncertain" (the evidence is thin or mixed), or "likely_false" (the evidence points against it). A sub-assumption nothing bears on is "uncertain", never "supported".
6. Decontextualization: identify up to 3 claims in the hypothesis that are bound to a specific context (a particular organism, cell line, dose, dataset, setting, or time window) and restate each in its general form — the broader claim the specific one is an instance of. For each, assess whether the general claim still holds, weakens, or changes meaning relative to the context-bound original. If the hypothesis is already fully general, leave the list empty.
7. Conclude with a verdict: "holds" (assumptions survive), "weakened" (non-fundamental gaps), or "undermined" (a fundamental assumption fails), plus a short overall_assessment.

Do not fill evidence gaps from confident prose. An unavailable or conflicting source must remain explicit uncertainty. Focus on correctness and the logical chain, not novelty or presentation.
