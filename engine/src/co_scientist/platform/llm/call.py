import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from co_scientist.platform.llm.admission.free_policy import scoped_api_key
from co_scientist.platform.llm.attempts.escalation import (
    BudgetEscalation,
    escalated_spec,
)
from co_scientist.platform.llm.attempts.json_attempt import (
    JsonJudge,
    _call_llm_single_attempt,
)
from co_scientist.platform.llm.attempts.retry import (
    Attempt,
    AttemptPlan,
    Judge,
    Rejected,
    run_attempts,
)
from co_scientist.platform.llm.request.thinking import scoped_minimal_reasoning
from co_scientist.platform.llm.roles import scoped_call_policy
from co_scientist.platform.llm.structured.validate import (
    _handle_json_retries_exhausted,
    extract_response_json,
)
from co_scientist.platform.llm.telemetry import logical_call_span
from co_scientist.platform.llm.values import CompletionSpec, LLMCallOptions

logger = logging.getLogger(__name__)


def _call_for_attempt(
    prompt: str, spec: CompletionSpec, opt: LLMCallOptions
) -> Callable[[Attempt], Awaitable[str]]:
    inner_opt = dataclasses.replace(opt, run_id=None, prompt_name=None)

    async def _attempt(attempt: Attempt) -> str:
        escalation = attempt.rung
        attempt_spec = escalated_spec(spec, escalation)
        attempt_opt = inner_opt
        if escalation is BudgetEscalation.NO_THINKING:
            attempt_opt = dataclasses.replace(inner_opt, enable_thinking=False)
        if escalation is BudgetEscalation.MINIMAL_REASONING_REQUIRED:
            attempt_opt = dataclasses.replace(inner_opt, enable_thinking=False)
            with scoped_minimal_reasoning():
                return await _call_llm_single_attempt(prompt, attempt_spec, attempt_opt)
        return await _call_llm_single_attempt(prompt, attempt_spec, attempt_opt)

    return _attempt


async def call_llm(
    prompt: str,
    spec: CompletionSpec,
    options: LLMCallOptions | None = None,
    max_attempts: int = 3,
) -> str:
    """Three attempts cover ordinary budget rungs; JSON needs extra room for
    parse/schema correction.
    """
    opt = options if options is not None else LLMCallOptions()
    with (
        logical_call_span("call_llm", spec.model_name, opt.prompt_name),
        scoped_api_key(spec.api_key),
        scoped_call_policy(
            opt.role or spec.role,
            opt.effort or spec.effort,
            enable_thinking=opt.enable_thinking,
            reasoning_max_tokens=opt.reasoning_max_tokens,
        ),
    ):
        return await run_attempts(
            _call_for_attempt(prompt, spec, opt),
            AttemptPlan(spec.model_name, max_attempts),
        )


async def _call_llm_for_json(
    prompt: str, spec: CompletionSpec, enable_thinking: bool = True
) -> str:
    """Never nest another attempt loop."""
    response_text = await _call_llm_single_attempt(
        prompt, spec, LLMCallOptions(enable_thinking=enable_thinking)
    )
    return extract_response_json(response_text)


def _json_call_for_attempt(
    json_spec: CompletionSpec, enable_thinking: bool, judge: JsonJudge
) -> Callable[[Attempt], Awaitable[str]]:
    """Resolve the raw-response seam in this module so installed test fakes
    still intercept it.
    """

    async def _call_for_json(attempt: Attempt) -> str:
        escalation = attempt.rung
        attempt_prompt = judge.prompt_for(attempt)
        call_enable_thinking = enable_thinking and escalation is not BudgetEscalation.NO_THINKING
        if escalation is BudgetEscalation.MINIMAL_REASONING_REQUIRED:
            with scoped_minimal_reasoning():
                return await _call_llm_for_json(
                    attempt_prompt,
                    escalated_spec(json_spec, escalation),
                    enable_thinking=False,
                )
        return await _call_llm_for_json(
            attempt_prompt,
            escalated_spec(json_spec, escalation),
            enable_thinking=call_enable_thinking,
        )

    return _call_for_json


def _json_judge(judge: JsonJudge, max_attempts: int) -> Judge[str, dict[str, Any]]:

    def exhausted(last: Rejected) -> dict[str, Any]:
        return _handle_json_retries_exhausted(
            judge.spec.json_schema, last.error, last.response_text, max_attempts
        )

    return Judge(judge.verdict, exhausted)


async def call_llm_json(
    prompt: str,
    spec: CompletionSpec,
    max_attempts: int = 5,
    options: LLMCallOptions | None = None,
) -> dict[str, Any]:
    opt = options if options is not None else LLMCallOptions()
    # Scope the explicit key over every attempt, without putting it in the
    # JSON spec.
    with (
        logical_call_span("call_llm_json", spec.model_name, opt.prompt_name),
        scoped_api_key(spec.api_key),
        scoped_call_policy(
            opt.role or spec.role,
            opt.effort or spec.effort,
            enable_thinking=opt.enable_thinking,
            reasoning_max_tokens=opt.reasoning_max_tokens,
        ),
    ):
        json_spec = dataclasses.replace(spec, api_key=None, force_json=not spec.json_schema)

        judge = JsonJudge(prompt, json_spec)
        return await run_attempts(
            _json_call_for_attempt(json_spec, opt.enable_thinking, judge),
            AttemptPlan(json_spec.model_name, max_attempts),
            _json_judge(judge, max_attempts),
        )
