from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from co_scientist.core.exceptions import (
    FreeModelEligibilityError,
    LLMCallBudgetExceededError,
)
from co_scientist.platform.llm.admission.free_policy import scoped_api_key
from co_scientist.platform.llm.attempts.escalation import (
    BudgetEscalation,
    escalated_max_tokens,
)
from co_scientist.platform.llm.attempts.retry import Attempt, AttemptPlan, run_attempts
from co_scientist.platform.llm.precall import _prepare_llm_call
from co_scientist.platform.llm.request.completion import (
    _acompletion_within_timeout,
    _apply_api_key,
    _apply_timeout,
)
from co_scientist.platform.llm.request.response import _extract_completion_content
from co_scientist.platform.llm.request.thinking import _apply_thinking_args
from co_scientist.platform.llm.roles import current_call_policy, scoped_call_policy
from co_scientist.platform.llm.telemetry import logical_call_span
from co_scientist.platform.llm.tool_effects import batch_by_effects
from co_scientist.platform.llm.tools.policy import (
    DEFAULT_TOOL_LOOP_TOKEN_BUDGET,
    _handoff_iteration,
    _handoff_message,
    _handoff_spend,
    _turns_remaining,
    closing_message,
    transcript_tokens,
)
from co_scientist.platform.llm.tools.transcript import (
    INVALID_ARGUMENTS_ERROR,
    _message_to_history_dict,
    elide_aged_evidence,
    elide_repeated_papers,
    elide_superseded_writes,
    normalize_tool_transcript,
    object_arguments,
)
from co_scientist.platform.llm.values import CompletionSpec, LLMCallOptions, LLMRequest

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_TURN = 16
MAX_CONCURRENT_TOOLS = 4


async def _execute_logged_tool(
    call: Any,
    executor: Callable[[Any], Awaitable[dict[str, Any]]],
    allowed_names: frozenset[str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    name = str(getattr(getattr(call, "function", None), "name", "unknown"))[:80]
    outcome = "returned"
    raw = getattr(getattr(call, "function", None), "arguments", None)
    actual_name = getattr(getattr(call, "function", None), "name", None)
    if allowed_names is not None and (
        not isinstance(actual_name, str) or actual_name not in allowed_names
    ):
        return {
            "role": "tool",
            "name": name,
            "tool_call_id": call.id,
            "content": json.dumps({"error": "Tool is not available in this operation"}),
        }
    if object_arguments(raw) is None:
        logger.info("tool_call name=%s outcome=invalid_arguments", name)
        return {
            "role": "tool",
            "name": name,
            "tool_call_id": call.id,
            "content": json.dumps({"error": INVALID_ARGUMENTS_ERROR}),
        }
    try:
        return await executor(call)
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "failed"
        raise
    finally:
        logger.info(
            "tool_call name=%s outcome=%s duration_seconds=%.3f",
            name,
            outcome,
            time.perf_counter() - started,
        )


async def _execute_tool_calls(
    tool_calls: list[Any],
    tool_executor: Callable[[Any], Awaitable[dict[str, Any]]],
    allowed_names: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Effect barriers run alone in model-requested order; batching must not
    reorder read/write effects.
    """
    results: list[dict[str, Any]] = []
    for batch in batch_by_effects(tool_calls[:MAX_TOOL_CALLS_PER_TURN]):
        for start in range(0, len(batch), MAX_CONCURRENT_TOOLS):
            results.extend(
                await asyncio.gather(
                    *[
                        _execute_logged_tool(tc, tool_executor, allowed_names)
                        for tc in batch[start : start + MAX_CONCURRENT_TOOLS]
                    ]
                )
            )
    results.extend(
        {
            "role": "tool",
            "name": str(getattr(getattr(call, "function", None), "name", "unknown"))[:80],
            "tool_call_id": call.id,
            "content": json.dumps({"error": "Tool call limit exceeded; use a later turn"}),
        }
        for call in tool_calls[MAX_TOOL_CALLS_PER_TURN:]
    )
    return results


def _build_tool_loop_completion_args(
    messages: list[dict[str, Any]],
    request: LLMRequest,
    escalation: BudgetEscalation = BudgetEscalation.NONE,
) -> dict[str, Any]:
    """Use the shared thinking floor and both timeout layers."""
    completion_args: dict[str, Any] = {
        "model": request.model_name,
        # Repair at send time so every cut transcript reaches the provider with
        # valid pairing.
        "messages": normalize_tool_transcript(messages),
        "max_tokens": escalated_max_tokens(request.max_tokens, escalation),
        "temperature": request.temperature,
        "drop_params": True,
    }
    if request.tools:
        completion_args["tools"] = request.tools
    _apply_thinking_args(
        completion_args,
        request.model_name,
        enable_thinking=(
            current_call_policy().enable_thinking and escalation is not BudgetEscalation.NO_THINKING
        )
        or escalation is BudgetEscalation.MINIMAL_REASONING_REQUIRED,
    )
    _apply_timeout(completion_args)
    _apply_api_key(completion_args)
    return completion_args


def _final_content(response: Any, model_name: str) -> str | None:
    message = response.choices[0].message
    if getattr(message, "tool_calls", None):
        return None
    return _extract_completion_content(response, model_name)


async def _answered_completion(
    messages: list[dict[str, Any]],
    request: LLMRequest,
) -> tuple[Any, str | None]:
    """Retries must stop before tool execution or repeat side effects.
    Reset rungs per turn to preserve the investigation step budget.
    """

    async def make_attempt(attempt: Attempt) -> tuple[Any, str | None]:
        response = await _acompletion_within_timeout(
            _build_tool_loop_completion_args(messages, request, attempt.rung),
            request.model_name,
        )
        return response, _final_content(response, request.model_name)

    return await run_attempts(make_attempt, AttemptPlan(request.model_name, max_attempts=3))


async def _run_tool_call_iteration(
    messages: list[dict[str, Any]],
    request: LLMRequest,
    tool_executor: Callable[[Any], Awaitable[dict[str, Any]]],
) -> tuple[bool, str | None]:
    """The attempt boundary logs because it knows whether a retry follows."""
    response, final = await _answered_completion(messages, request)
    message = response.choices[0].message

    if final is None:
        logger.debug("llm requested %s tool calls", len(message.tool_calls))
        messages.append(_message_to_history_dict(message))
        allowed_names = frozenset(
            tool["function"]["name"]
            for tool in request.tools or []
            if isinstance(tool.get("function", {}).get("name"), str)
        )
        messages.extend(await _execute_tool_calls(message.tool_calls, tool_executor, allowed_names))
        return False, None

    # Do not retain answerless assistant turns: the answering retry would resend
    # an empty message.
    messages.append(_message_to_history_dict(message))
    return True, final


async def _answer_without_tools(
    messages: list[dict[str, Any]],
    request: LLMRequest,
) -> str | None:
    """Withholding tools turns paid investigation into prose, not more actions.
    Provider-call budget errors must propagate, never become empty fallbacks.
    """
    closing = replace(request, tools=[])
    args = _build_tool_loop_completion_args([*messages, closing_message()], closing)
    args.pop("tools", None)
    try:
        response = await _acompletion_within_timeout(args, request.model_name)
        return _extract_completion_content(response, request.model_name)
    except (LLMCallBudgetExceededError, FreeModelEligibilityError):
        raise
    except Exception as exc:
        logger.warning("Could not harvest a final answer: %s", exc)
        return None


def _raise_budget_exhausted(loop: ToolLoop) -> NoReturn:
    logger.warning(
        "Tool call loop gave no final answer within %s iterations / %s prompt tokens",
        loop.max_iterations,
        loop.max_prompt_tokens,
    )
    raise RuntimeError(
        "LLM tool call loop exhausted its budget"
        f" (max_iterations={loop.max_iterations},"
        f" max_prompt_tokens={loop.max_prompt_tokens})"
    )


def _spend_exhausted(spent: int, loop: ToolLoop, iteration: int) -> bool:
    if spent < loop.max_prompt_tokens:
        return False
    logger.warning(
        "Token budget (%s) reached in tool call loop after %s iterations; %s tokens re-sent",
        loop.max_prompt_tokens,
        iteration,
        spent,
    )
    return True


def _handoff_due(
    iteration: int,
    handoff_at: int,
    spent: int,
    loop: ToolLoop,
    handed_off: bool,
) -> bool:
    if iteration == handoff_at:
        return True
    return not handed_off and spent >= _handoff_spend(loop.max_prompt_tokens)


def _drop_dead_context(messages: list[dict[str, Any]]) -> None:
    """Age before deduplication so old stubs cannot erase fresh evidence.
    Count, send and return the same transcript to avoid billing unsent text.
    """
    elided = elide_superseded_writes(messages)
    if elided:
        logger.debug("elided %s superseded file write(s)", elided)
    aged = elide_aged_evidence(messages)
    if aged:
        logger.debug("elided %s aged evidence result(s)", aged)
    repeats = elide_repeated_papers(messages)
    if repeats:
        logger.debug("elided %s repeated paper record(s)", repeats)


async def _harvest_partial_answer(
    request: LLMRequest,
    messages: list[dict[str, Any]],
    loop: ToolLoop,
) -> tuple[str, list[dict[str, Any]]]:
    answer = await _answer_without_tools(messages, request)
    if not answer:
        _raise_budget_exhausted(loop)
    logger.info(
        "Tool call loop ran out of room; harvested a %s-character partial answer",
        len(answer),
    )
    return answer, messages


async def _run_tool_call_loop(
    request: LLMRequest,
    messages: list[dict[str, Any]],
    tool_executor: Callable[[Any], Awaitable[dict[str, Any]]],
    loop: ToolLoop,
) -> tuple[str, list[dict[str, Any]]]:
    """Turns resend the whole transcript, so a turn count alone cannot bound
    spend.
    """
    max_iterations = loop.max_iterations
    handoff_at = _handoff_iteration(max_iterations)
    handed_off = False
    spent = 0
    for iteration in range(max_iterations):
        logger.debug("llm tool call iteration %s/%s", iteration + 1, max_iterations)
        _drop_dead_context(messages)
        spent += transcript_tokens(messages)
        if _spend_exhausted(spent, loop, iteration):
            return await _harvest_partial_answer(request, messages, loop)
        if _handoff_due(iteration, handoff_at, spent, loop, handed_off):
            handed_off = True
            messages.append(_handoff_message(_turns_remaining(iteration, spent, loop, messages)))
        done, final_content = await _run_tool_call_iteration(messages, request, tool_executor)
        if done:
            assert final_content is not None
            logger.debug("llm finished after %s iterations", iteration + 1)
            return final_content, messages

    return await _harvest_partial_answer(request, messages, loop)


@dataclass(frozen=True)
class ToolLoop:
    """A closing turn can exceed local bounds but never the provider cap."""

    tools: list[dict[str, Any]]
    executor: Callable[[Any], Awaitable[dict[str, Any]]]
    max_iterations: int = 10
    max_prompt_tokens: int = DEFAULT_TOOL_LOOP_TOKEN_BUDGET
    tool_contract: dict[str, Any] | None = None


async def call_llm_with_tools(
    prompt: str,
    spec: CompletionSpec,
    loop: ToolLoop,
    options: LLMCallOptions | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    opt = options if options is not None else LLMCallOptions()
    # An explicit key overrides this task context for the loop, not shared
    # backend state.
    with (
        logical_call_span("call_llm_with_tools", spec.model_name, opt.prompt_name),
        scoped_api_key(spec.api_key),
        scoped_call_policy(
            opt.role or spec.role,
            opt.effort or spec.effort,
            enable_thinking=opt.enable_thinking,
            reasoning_max_tokens=opt.reasoning_max_tokens,
        ),
    ):
        request = LLMRequest(
            prompt=prompt,
            model_name=spec.model_name,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            tools=loop.tools,
            tool_contract=loop.tool_contract,
        )
        request = _prepare_llm_call(request)
        messages = [{"role": "user", "content": prompt}]
        return await _run_tool_call_loop(request, messages, loop.executor, loop)
