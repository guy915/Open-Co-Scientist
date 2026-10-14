from __future__ import annotations

from typing import Any

import pytest
from co_scientist.domains.chat import qa


@pytest.mark.asyncio
async def test_qa_whitespace_after_reasoning_gets_one_answer_retry(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    requests: list[dict[str, Any]] = []

    async def completion(request: dict[str, Any], tool_calls: dict[int, dict[str, Any]]) -> Any:
        requests.append(request)
        if len(requests) == 1:
            yield "reasoning", "considering the evidence"
            yield "chunk", " \n "
        else:
            yield "chunk", "The evidence is insufficient."

    monkeypatch.setattr(qa, "_stream_completion", completion)
    frames = [
        frame async for frame in qa.stream_llm_deltas("deepseek/deepseek-chat", "sys", "q", [])
    ]
    assert len(requests) == 2
    assert frames[-1] == ("chunk", "The evidence is insufficient.")


@pytest.mark.parametrize(
    ("prose", "reasoned", "tool_requested", "expected"),
    [
        ("", False, False, [True]),
        (" \n ", True, False, [True, False]),
        ("partial", True, False, [True]),
        ("", True, True, [True]),
    ],
)
def test_retry_policy_keeps_silence_prose_and_tools_distinct(
    prose: str, reasoned: bool, tool_requested: bool, expected: list[bool]
) -> None:
    from co_scientist.platform.llm.stream import ReasoningRetry

    retry = ReasoningRetry()
    attempts = []
    for thinking in retry.attempts():
        attempts.append(thinking)
        retry.observe(prose=prose, reasoned=reasoned, tool_requested=tool_requested)
    assert attempts == expected


@pytest.mark.asyncio
async def test_qa_retry_that_only_reasons_does_not_make_a_third_request(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    requests = []

    async def completion(request: dict[str, Any], tool_calls: dict[int, dict[str, Any]]) -> Any:
        requests.append(request)
        yield "reasoning", "still considering"

    monkeypatch.setattr(qa, "_stream_completion", completion)
    frames = [
        frame async for frame in qa.stream_llm_deltas("deepseek/deepseek-chat", "sys", "q", [])
    ]
    assert len(requests) == 2
    assert len(frames) == 2


@pytest.mark.parametrize("refusal", ["policy refusal", None])
@pytest.mark.asyncio
async def test_goal_refusal_after_reasoning_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, refusal: str | None
) -> None:
    from types import SimpleNamespace

    from co_scientist.domains.chat import goal_text

    calls = []

    async def completion(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="", refusal=refusal),
                    finish_reason="content_filter" if refusal is None else "stop",
                )
            ],
            usage=SimpleNamespace(completion_tokens_details=SimpleNamespace(reasoning_tokens=10)),
        )

    monkeypatch.setattr(goal_text, "_request_completion", completion)
    assert await goal_text.generate_run_title("a scientific goal") is None
    assert len(calls) == 1


def test_streamed_refusal_is_terminal_after_reasoning() -> None:
    from types import SimpleNamespace

    from co_scientist.core.exceptions import LLMContentFilteredError
    from co_scientist.domains.chat.run_start_announcement import _delta_text

    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="", reasoning_content="thought", refusal="refused"),
                finish_reason="stop",
            )
        ]
    )
    with pytest.raises(LLMContentFilteredError):
        _delta_text(chunk)


@pytest.mark.asyncio
async def test_qa_interrupted_reasoning_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    calls = []

    async def completion(request: dict[str, Any], tool_calls: dict[int, dict[str, Any]]) -> Any:
        calls.append(request)
        yield "reasoning", "considering"
        raise TimeoutError("stream interrupted")

    monkeypatch.setattr(qa, "_stream_completion", completion)
    with pytest.raises(TimeoutError):
        async for _ in qa.stream_llm_deltas("deepseek/deepseek-chat", "sys", "q", []):
            pass
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_qa_incomplete_tool_request_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    calls = []

    async def completion(request: dict[str, Any], tool_calls: dict[int, dict[str, Any]]) -> Any:
        calls.append(request)
        tool_calls[0] = {"arguments": "{"}
        yield "reasoning", "looking up evidence"

    monkeypatch.setattr(qa, "_stream_completion", completion)
    frames = [
        frame async for frame in qa.stream_llm_deltas("deepseek/deepseek-chat", "sys", "q", [])
    ]
    assert len(calls) == 1
    assert frames == [("reasoning", "looking up evidence")]


def test_announcement_tool_request_cannot_trigger_an_answer_retry() -> None:
    from types import SimpleNamespace

    from co_scientist.domains.chat.run_start_announcement import _delta_text

    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content="", reasoning_content="thought", tool_calls=[{"id": "call"}]
                ),
                finish_reason="tool_calls",
            )
        ]
    )
    with pytest.raises(ValueError, match="Unexpected tool request"):
        _delta_text(chunk)
