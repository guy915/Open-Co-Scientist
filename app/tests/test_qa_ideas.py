from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.domains.chat import qa

from tests._llm_fake_backend import install_completion_backend


def _idea(title: str, **fields: Any) -> dict[str, Any]:
    base = {
        "title": title,
        "statement": "a statement",
        "mechanism": "a mechanism",
        "elo_rating": 1200,
        "status": "active",
    }
    return {**base, **fields}


def _fragment(index: int, **fields: Any) -> SimpleNamespace:
    function = SimpleNamespace(name=fields.get("name"), arguments=fields.get("arguments"))
    return SimpleNamespace(index=index, id=fields.get("id"), function=function)


def _chunk(content: str | None = None, tool_calls: Any = None) -> Any:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _scripted_litellm(rounds: list[list[Any]]) -> Any:
    sent: list[dict[str, Any]] = []

    async def _acompletion(**kwargs: Any) -> Any:
        sent.append(kwargs)

        async def _stream() -> Any:
            for chunk in rounds[len(sent) - 1]:
                yield chunk

        return _stream()

    return SimpleNamespace(acompletion=_acompletion, sent=sent)


def _drain(generator: Any) -> list[tuple[str, str]]:
    import asyncio

    async def _collect() -> list[tuple[str, str]]:
        return [delta async for delta in generator]

    return asyncio.run(_collect())


def _search_call_chunk() -> Any:
    return _chunk(
        tool_calls=[
            _fragment(
                0,
                id="c1",
                name="search_ideas",
                arguments='{"query": "lipid"}',
            )
        ]
    )


def test_a_model_that_asks_for_ideas_is_given_them_and_answers(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    fake = _scripted_litellm([[_search_call_chunk()], [_chunk("Idea one says X.")]])
    install_completion_backend(monkeypatch, (fake).acompletion)

    deltas = _drain(
        qa.stream_llm_deltas("model", "sys", "what does it say?", [_idea("Lipid repair")])
    )

    assert deltas == [("chunk", "Idea one says X.")]
    second = fake.sent[1]
    tool_message = second["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "Lipid repair" in tool_message["content"]
    assert "tools" not in second
    assert fake.sent[0]["max_tokens"] == second["max_tokens"]
    assert fake.sent[0]["timeout"] == second["timeout"]


def test_run_artifact_lookup_uses_same_bounded_two_round_stream(
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
) -> None:
    scripted = _scripted_litellm(
        [
            [
                _chunk(
                    tool_calls=[
                        _fragment(
                            0,
                            id="run_lookup",
                            name="search_run_artifacts",
                            arguments='{"section":"meta_review"}',
                        )
                    ]
                )
            ],
            [_chunk(content="Replication remains necessary.")],
        ]
    )
    install_completion_backend(monkeypatch, scripted.acompletion)
    output = _drain(
        qa.stream_llm_deltas(
            "model",
            "sys",
            "What did the synthesis conclude?",
            [],
            {"meta_review": [{"conclusion": "Replication remains necessary"}]},
        )
    )
    assert output == [("chunk", "Replication remains necessary.")]
    assert len(scripted.sent) == 2
    assert scripted.sent[0]["tools"][0]["function"]["name"] == "search_run_artifacts"
    assert "tools" not in scripted.sent[1]
    messages = scripted.sent[1]["messages"]
    assert messages[-1]["tool_call_id"] == "run_lookup"
    assert "Replication remains necessary" in messages[-1]["content"]


# A model whose profile honours an explicit disable, so the retry's request
# shape is observable.
_THINKING_MODEL = "deepseek/deepseek-r1"


def _reasoning_chunk(text: str) -> Any:
    delta = SimpleNamespace(content=None, tool_calls=None, reasoning_content=text)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def test_a_turn_that_reasons_and_writes_nothing_is_retried_without_thinking(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    """The interview and the announcement already retry this case once. Q&A
    used to persist the empty answer instead.
    """
    fake = _scripted_litellm(
        [[_reasoning_chunk("thinking at length")], [_chunk("The report says X.")]]
    )
    install_completion_backend(monkeypatch, fake.acompletion)

    deltas = _drain(qa.stream_llm_deltas(_THINKING_MODEL, "sys", "what does it say?", []))

    assert ("chunk", "The report says X.") in deltas
    assert len(fake.sent) == 2
    # The retry re-asks the same question with thinking off, as the interview
    # and the announcement paths do.
    assert fake.sent[1]["messages"] == fake.sent[0]["messages"]
    assert fake.sent[0]["extra_body"]["thinking"] == {"type": "enabled"}
    assert fake.sent[1]["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in fake.sent[1]


def test_a_turn_that_writes_prose_is_never_retried(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    fake = _scripted_litellm([[_reasoning_chunk("brief thought"), _chunk("An answer.")]])
    install_completion_backend(monkeypatch, fake.acompletion)

    deltas = _drain(qa.stream_llm_deltas(_THINKING_MODEL, "sys", "what does it say?", []))

    assert deltas == [("reasoning", "brief thought"), ("chunk", "An answer.")]
    assert len(fake.sent) == 1
