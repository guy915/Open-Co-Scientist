# ADR-004: LLM gateway

**Status:** Accepted.

## Context

Model calls take two paths, and the policy that decides whether and how a call
is sent (BYOK, offline decision, zero-price admission, output caps, daily
usage, thinking and routing arguments) must apply to both.

## Decision

**`co_scientist.platform.llm` is the gateway.** Every model call in the
product goes through it, and `litellm` is imported only inside it (the
`litellm` contract in `.importlinter`).

**Science calls** use `call_llm`, `call_llm_json` and `call_llm_with_tools`
(`call.py`, `tools/loop.py`). They run the attempt ladder (`attempts/`: retry,
budget escalation, JSON repair) over `complete_request`
(`request/transport.py`), the single physical-call seam: it applies provider
constraints, zero-price admission and the physical-call reservation, and
opens the request span.

**Surface calls** (chat, interviews, Q&A, start announcement, goal text, BYOK
validation) use `llm_request.acompletion`. It opens an `app_call_scope`
(`llm_scope.py`: surface call cap and telemetry), refuses a remote call when
the process is offline and no BYOK credential is scoped (`offline_guard.py`),
applies the surface output cap, then calls the same `complete_request`.
`validate_byok_credential` is in `domains/access/credentials.py` and calls
`acompletion`.

**Context a caller opens around its calls**, exported from the package
`__init__`: `scoped_zero_cost_admission`, `scoped_api_key`,
`scoped_completion_budget`, `scoped_llm_call_budget` and `scoped_telemetry`
(plus `scoped_telemetry_phase`). `execution_policy.py` and `process_mode.py`
decide zero-cost admission and offline mode. The inputs are `CompletionSpec`
and `LLMCallOptions` (`values.py`) and `ToolLoop`.

**Model facts and routing.** Each model is one `ModelProfile` in
`platform/llm/profile/` (capabilities, routing pin and fallbacks, price);
`model_profile(name)` reads it. `request/thinking.py` holds the policy applied
to a profile: reasoning and thinking arguments, `effective_max_tokens`,
provider pins and constraints.

**Export mechanics.** The package `__init__` resolves its exports lazily
through `__getattr__` and a `_EXPORTS` table, because eager imports re-enter
foundation modules while they are half-initialized.

**What stays outside:** who may run what (free allowance, ownership, BYOK
storage) is `domains/access`; the gateway receives the decision as context
(`scoped_api_key`, zero-cost admission) and does not read those tables.
Prompts and schemas belong to the science agents; the gateway sees the
rendered prompt and the schema.

**Invariants it owns** (from `docs/OPERATIONS.md`): bounded provider calls,
token and reasoning budget escalation within the profile's ceiling, spend
caps, zero-price admission for free runs, free routes without paid fallbacks,
one physical-call seam with no retry of its own, failure logs at the retry
boundary only, and offline isolation.

## Consequences

- Tracing (ADR-005) attaches in one place: the logical call, the attempt
  ladder and `complete_request`.
- Callers outside the gateway never see provider details or import `litellm`.
- `engine/tests/test_agents.py` orders the gateway's modules and subpackages
  (`profile`, `values`, `admission`, ..., `llm_request`) as its own layering
  rule.
- The lazy export table must be updated with each new public name.
