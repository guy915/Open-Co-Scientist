from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from co_scientist.core import constants
from co_scientist.core.constants import (
    BUDGET_ESCALATION_MAX_INCREMENT,
    BUDGET_ESCALATION_MAX_TOKENS,
    THINKING_FLOOR_MAX_TOKENS,
)
from co_scientist.core.exceptions import (
    LLMBudgetExhaustedError,
    LLMCallBudgetExceededError,
    LLMThinkingOnlyError,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    ToolLoop,
    call_llm,
    call_llm_json,
    call_llm_with_tools,
    current_run_call_count,
    release_run_call_budget,
    scoped_llm_call_budget,
)
from co_scientist.platform.llm.admission.call_budget import (
    _MAX_TRACKED_RUNS,
    current_completion_budget,
    record_provider_request,
    scoped_completion_budget,
)
from co_scientist.platform.llm.attempts.escalation import (
    BudgetEscalation,
    escalated_max_tokens,
)
from co_scientist.platform.llm.request.response import _extract_completion_content
from co_scientist.science.supervisor.orchestrator import _compute_stats
from tests._llm_fake import (
    SEARCH_TOOL,
    echo_executor,
    make_tool_call,
    scripted_backend,
)
from tests._llm_fake import make_completion as _completion
from tests._llm_fake import make_message as _message
from tests._llm_fake import make_usage as _usage

_MODEL = "deepseek/deepseek-v4-flash"
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
}
_JSON_SPEC = CompletionSpec(model_name=_MODEL, json_schema=_SCHEMA)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


def _exhausted(budget: int) -> SimpleNamespace:
    return _completion(
        _message(None),
        usage=_usage(500, budget, reasoning_tokens=budget),
        finish_reason="length",
    )


def _thinking_only(reasoning: int) -> SimpleNamespace:
    """More room cannot fix thinking-only output; recovery must change
    reasoning."""
    return _completion(
        _message(None),
        usage=_usage(3136, reasoning, reasoning_tokens=reasoning),
        finish_reason="stop",
    )


def _provider_error(reasoning: int) -> SimpleNamespace:
    """Transport failures must not be mistaken for reasoning exhaustion."""
    return _completion(
        _message(None),
        usage=_usage(3378, reasoning, reasoning_tokens=reasoning),
        finish_reason="error",
    )


def _empty_without_reasoning() -> SimpleNamespace:
    return _completion(_message("   "), usage=_usage(500, 0), finish_reason="stop")


def _requests(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Any],
    *,
    repeat_last: bool = True,
) -> list[dict[str, Any]]:
    return scripted_backend(monkeypatch, responses, repeat_last=repeat_last).requests


def _thinking(requests: list[dict[str, Any]]) -> list[str]:
    return [call["extra_body"]["thinking"]["type"] for call in requests]


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (_exhausted(THINKING_FLOOR_MAX_TOKENS), LLMBudgetExhaustedError),
        (_thinking_only(1149), LLMThinkingOnlyError),
        (_empty_without_reasoning(), ValueError),
        (_provider_error(519), ValueError),
    ],
    ids=["budget-spent", "thinking-only", "empty-reply", "mid-stream-error"],
)
async def test_an_answerless_reply_is_named_for_what_went_wrong(
    monkeypatch: pytest.MonkeyPatch, response: Any, error_type: type[Exception]
) -> None:
    _requests(monkeypatch, [response])

    with pytest.raises(ValueError) as caught:
        await call_llm("a prompt", CompletionSpec(model_name=_MODEL))

    assert type(caught.value) is error_type, (
        "only a reply that spent reasoning is escalated; a transport hiccup "
        "or empty reply stays an ordinary error"
    )


async def test_the_ladder_raises_the_budget_then_drops_thinking_and_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _requests(monkeypatch, [_exhausted(THINKING_FLOOR_MAX_TOKENS)])

    with pytest.raises(LLMBudgetExhaustedError):
        await call_llm_json(
            "a prompt",
            CompletionSpec(model_name=_MODEL, max_tokens=8000, json_schema=_SCHEMA),
            max_attempts=5,
        )

    assert [call["max_tokens"] for call in calls] == [
        THINKING_FLOOR_MAX_TOKENS,
        BUDGET_ESCALATION_MAX_TOKENS,
        *[BUDGET_ESCALATION_MAX_TOKENS] * 3,
    ]
    assert _thinking(calls) == ["enabled", "enabled"] + ["disabled"] * 3


async def test_a_call_already_sized_at_the_constant_still_gets_more_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeating an identical exhausted budget buys another identical
    failure."""
    calls = _requests(monkeypatch, [_exhausted(BUDGET_ESCALATION_MAX_TOKENS)])

    with pytest.raises(LLMBudgetExhaustedError):
        await call_llm_json(
            "a prompt",
            CompletionSpec(
                model_name=_MODEL,
                max_tokens=BUDGET_ESCALATION_MAX_TOKENS,
                json_schema=_SCHEMA,
            ),
            max_attempts=3,
        )

    budgets = [call["max_tokens"] for call in calls]
    assert budgets[0] == BUDGET_ESCALATION_MAX_TOKENS
    assert budgets[1] > BUDGET_ESCALATION_MAX_TOKENS
    assert budgets[2] == budgets[1]


async def test_a_content_filtered_reply_retries_without_changing_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filtered = _completion(
        _message(None),
        usage=_usage(3136, 1149, reasoning_tokens=1149),
        finish_reason="content_filter",
    )
    calls = _requests(monkeypatch, [filtered, _completion(_message('{"a":2}'))])

    result = await call_llm_json("a prompt", _JSON_SPEC, max_attempts=5)

    assert result == {"a": 2}
    assert _thinking(calls) == ["enabled", "enabled"]
    assert calls[0]["max_tokens"] == calls[1]["max_tokens"]


async def test_a_thinking_only_reply_skips_the_budget_rung_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _requests(
        monkeypatch,
        [_thinking_only(1149), _completion(_message('{"a":2}'))],
    )

    result = await call_llm_json("a prompt", _JSON_SPEC, max_attempts=5)

    assert result == {"a": 2}
    assert _thinking(calls) == ["enabled", "disabled"]
    assert "reasoning_effort" not in calls[1]


async def test_a_provider_error_recovers_without_disabling_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _requests(monkeypatch, [_provider_error(519), _completion(_message('{"a":3}'))])

    result = await call_llm_json("a prompt", _JSON_SPEC, max_attempts=5)

    assert result == {"a": 3}
    assert _thinking(calls) == ["enabled", "enabled"]


@pytest.mark.parametrize(
    ("max_tokens", "rung", "expected"),
    [
        (8000, BudgetEscalation.RAISED_BUDGET, BUDGET_ESCALATION_MAX_TOKENS),
        (24000, BudgetEscalation.RAISED_BUDGET, 36000),
        (200_000, BudgetEscalation.RAISED_BUDGET, 200_000 + 24000),
        (8000, BudgetEscalation.NONE, 8000),
    ],
)
def test_a_raised_budget_never_cuts_a_larger_caller_down(
    max_tokens: int, rung: BudgetEscalation, expected: int
) -> None:
    assert escalated_max_tokens(max_tokens, rung) == expected
    assert expected - max_tokens <= BUDGET_ESCALATION_MAX_INCREMENT


def test_every_scaled_cap_clears_the_thinking_floor() -> None:
    """A cap below the floor is overwritten wherever the model reasons."""
    caps = {
        name: value
        for name, value in vars(constants).items()
        if name.endswith("_MAX_TOKENS_CAP") and isinstance(value, int)
    }

    assert caps, "no *_MAX_TOKENS_CAP constants found; did they get renamed?"
    assert not {n: v for n, v in caps.items() if v <= THINKING_FLOOR_MAX_TOKENS}


def test_a_run_without_an_id_is_never_counted_or_limited() -> None:
    record_provider_request()
    with scoped_llm_call_budget(None, ceiling=1):
        for _ in range(3):
            record_provider_request()


def test_exceeding_the_ceiling_raises_with_count_and_ceiling() -> None:
    run_id = "run-over-1"
    with scoped_llm_call_budget(run_id, ceiling=2):
        record_provider_request()
        record_provider_request()
        with pytest.raises(LLMCallBudgetExceededError) as excinfo:
            record_provider_request()
    assert (excinfo.value.count, excinfo.value.ceiling) == (3, 2)
    release_run_call_budget(run_id)


def test_reentering_a_run_keeps_its_count_and_its_first_ceiling() -> None:
    run_id = "run-reenter-ceiling"
    with scoped_llm_call_budget(run_id, ceiling=2):
        record_provider_request()
    with scoped_llm_call_budget(run_id, ceiling=1000):
        record_provider_request()
        assert current_run_call_count(run_id) == 2
        with pytest.raises(LLMCallBudgetExceededError):
            record_provider_request()
    release_run_call_budget(run_id)


def test_two_concurrent_runs_are_counted_independently() -> None:
    results: dict[str, int] = {}

    def drive(run_id: str, n: int) -> None:
        with scoped_llm_call_budget(run_id, ceiling=None):
            for _ in range(n):
                record_provider_request()
        results[run_id] = current_run_call_count(run_id)

    threads = [
        threading.Thread(target=drive, args=("run-a", 5)),
        threading.Thread(target=drive, args=("run-b", 9)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == {"run-a": 5, "run-b": 9}
    release_run_call_budget("run-a")
    release_run_call_budget("run-b")


def test_releasing_a_run_drops_its_counter_and_an_unseen_run_is_a_no_op() -> None:
    run_id = "run-release-1"
    with scoped_llm_call_budget(run_id, ceiling=None):
        record_provider_request()
    assert current_run_call_count(run_id) == 1
    release_run_call_budget(run_id)
    assert current_run_call_count(run_id) == 0
    release_run_call_budget("run-never-seen")


def test_the_tracking_cap_evicts_the_oldest_run() -> None:
    run_ids = [f"run-evict-{i}" for i in range(_MAX_TRACKED_RUNS + 1)]
    try:
        for run_id in run_ids:
            with scoped_llm_call_budget(run_id, ceiling=None):
                record_provider_request()
        assert current_run_call_count(run_ids[0]) == 0
        assert current_run_call_count(run_ids[-1]) == 1
    finally:
        for run_id in run_ids:
            release_run_call_budget(run_id)


def _asks_for_a_tool() -> SimpleNamespace:
    return _completion(_message(None, tool_calls=[make_tool_call("c1", "search", "{}")]))


@pytest.mark.parametrize("kind", ["text", "json", "tools"])
async def test_a_spent_ceiling_stops_a_call_before_it_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    requests = _requests(monkeypatch, [_completion(_message('{"a": 1}'))])
    run_id = f"run-ceiling-{kind}"

    with (
        scoped_llm_call_budget(run_id, ceiling=0),
        pytest.raises(LLMCallBudgetExceededError),
    ):
        if kind == "text":
            await call_llm("a prompt", CompletionSpec(model_name=_MODEL))
        elif kind == "json":
            await call_llm_json("a prompt", _JSON_SPEC, max_attempts=5)
        else:
            await call_llm_with_tools(
                "a prompt",
                CompletionSpec(model_name=_MODEL),
                ToolLoop(tools=SEARCH_TOOL, executor=echo_executor),
            )

    assert requests == []
    release_run_call_budget(run_id)


async def test_every_provider_attempt_and_tool_turn_counts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ok = _completion(_message('{"a": 1}'))
    _requests(
        monkeypatch,
        [ValueError("blip"), ValueError("blip"), ok],
        repeat_last=False,
    )
    with scoped_llm_call_budget("run-json-attempts", ceiling=None):
        assert await call_llm_json("a prompt", _JSON_SPEC, max_attempts=5) == {"a": 1}
    assert current_run_call_count("run-json-attempts") == 3
    release_run_call_budget("run-json-attempts")

    _requests(
        monkeypatch,
        [_asks_for_a_tool(), _completion(_message("final answer"))],
        repeat_last=False,
    )
    with scoped_llm_call_budget("run-tool-loop", ceiling=None):
        text, _ = await call_llm_with_tools(
            "a prompt",
            CompletionSpec(model_name=_MODEL, max_tokens=8000),
            ToolLoop(tools=SEARCH_TOOL, executor=echo_executor),
        )
    assert text == "final answer"
    assert current_run_call_count("run-tool-loop") == 2
    release_run_call_budget("run-tool-loop")


def test_the_between_task_check_reads_the_seam_count() -> None:
    run_id = "run-between-tasks"
    with scoped_llm_call_budget(run_id, ceiling=None):
        for _ in range(7):
            record_provider_request()
    try:
        state = cast(WorkflowState, {"hypotheses": [], "run_id": run_id})
        assert _compute_stats(state, {}).llm_calls == 7
    finally:
        release_run_call_budget(run_id)


async def test_a_completion_budget_is_shared_by_the_calls_it_spawns() -> None:
    invalid = scoped_completion_budget(0)
    assert current_completion_budget() is None
    with scoped_completion_budget(2) as outer:
        with pytest.raises(ValueError, match="at least one call"), invalid:
            pass
        assert current_completion_budget() is outer

        async def consume() -> None:
            assert current_completion_budget() is outer
            record_provider_request()

        await asyncio.gather(consume(), consume())
        with pytest.raises(LLMCallBudgetExceededError):
            record_provider_request()
    assert current_completion_budget() is None


def test_an_empty_answer_names_the_model_that_served_it() -> None:
    response = _thinking_only(0)
    response.model = "dots-studio/dots-3-note-preview:free"
    requested = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"

    with pytest.raises(ValueError, match="served dots-studio") as caught:
        _extract_completion_content(response, requested)

    assert requested in str(caught.value)


def test_an_answer_from_a_declared_fallback_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requested = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    primary = _completion(_message("{}"))
    primary.model = "nvidia/nemotron-3-ultra-550b-a55b:free"
    fallback = _completion(_message("{}"))
    fallback.model = "nvidia/nemotron-3-super-120b-a12b:free"

    with caplog.at_level(logging.INFO):
        _extract_completion_content(primary, requested)
        assert "fallback" not in caplog.text
        _extract_completion_content(fallback, requested)

    assert (
        f"LLM call to {requested} was answered by fallback nvidia/nemotron-3-super-120b-a12b:free"
    ) in caplog.text
