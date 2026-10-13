# Model call roles

`CompletionSpec` and `LLMCallOptions` carry a role and optional effort.
Options override the spec. The role scope covers every retry, tool turn and
closing turn. App calls pass `call_role` and optional `call_effort`.

The table below is the one policy for every provider. A role with effort none
never reasons, and a caller can also turn thinking off for one call. Every
request gets its reasoning fields from one dispatch step
(`_apply_thinking_args`, then `apply_provider_constraints`), and `wire_effort`
maps a reasoning call's effort to what each provider is sent:

| Provider | Reasoning call | No reasoning |
|---|---|---|
| Free OpenRouter route | max (bills per request); chat and interview keep the table effort | reasoning off, or capped at its minimum where the model cannot turn it off |
| Paid OpenRouter route | table effort (bills per token) | as above |
| DeepSeek API | high, its lowest tier | thinking disabled |
| Azure Luna (operator credit) | low; supervisor and overview medium | none |
| Direct Haiku (operator credit) | adaptive thinking at effort low | thinking disabled |

Haiku 5.5 accepts only adaptive or disabled thinking; effort sets how deep the
adaptive thinking goes. A model whose profile has no reasoning switch is sent
none and keeps its own default. BYOK keeps its selected models and credentials.

| Role | Tier | Default effort |
|---|---|---|
| supervisor, meta_review, overview | Supervisor | medium |
| overview_review, overview_outline, overview_directions | Supervisor | low |
| worker, generation, literature_synthesis, reflection, review, deep_verification, simulation, evolution, interview, chat | Worker | medium |
| orchestrator, research_extract, literature_analysis, drafting, novelty, ranking, proximity, relevance, safety | Worker | low |
| evidence_queries, grounding_queries, literature_queries, research, claims, question_repair, goal_text, announcement, credential_probe | Worker | none |

Set `LLM_EFFORT_<ROLE>` to override a named role, for example
`LLM_EFFORT_RANKING=low`. Otherwise an explicit call effort applies, followed by
`LLM_WORKER_EFFORT` or `LLM_SUPERVISOR_EFFORT`, then the table default. Environment
values are read per request. An override turns a role's reasoning on or off for
every provider and sets its effort on Luna and paid routes. Only none, low and
medium are accepted. A retry that disables thinking uses none on Azure.

Exact USD prices and model versions live in `platform/llm/profile/`. Azure
deployment names do not define prices. Luna includes cache-write and
long-context rates. Its published boundary is 272,000 input tokens: a request
above it pays the long rates for every token. The ledger prices a call at the
short rates when its input bound or reported prompt is at or below the boundary.

Production operator routing selects the native Responses client for Azure.
`LLM_AZURE_ENABLED`, a positive hard total and an unexpired cutoff are required.
The subscriber API slot always uses low effort. Its output ceilings include
hidden thinking.

Operator short roles have a 180-second deadline; overview, meta review and
literature analysis have 600 seconds. Unknown transport outcomes retain spend
and must not be replayed by an outer retry.
