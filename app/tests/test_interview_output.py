from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from co_scientist.domains.chat.interviews.model import TurnSplitter


@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize("first", ["research_challenge", "questions"])
def test_streamed_metadata_never_reaches_prose(fenced: bool, first: str) -> None:
    fields = {
        first: "Study salt stress" if first == "research_challenge" else [],
        "research_challenge": "Study salt stress",
        "focus_area": [],
        "preferences": [],
        "lab_constraints": [],
        "completed": False,
    }
    payload = json.dumps(fields)
    if fenced:
        payload = "```json\n" + payload + "\n```"
    text = "Which mechanism should we focus on?\n\n" + payload
    splitter = TurnSplitter()
    emitted = ""
    for character in text:
        emitted += splitter.feed(character)
        assert '"research_challenge"' not in emitted
        assert '"questions"' not in emitted
        assert "```" not in emitted
    trailing, prose, spec = splitter.finish()
    assert (emitted + trailing).strip() == "Which mechanism should we focus on?"
    assert prose == "Which mechanism should we focus on?"
    assert spec == fields


def test_question_json_before_canonical_spec_is_hidden_without_losing_prose() -> None:
    questions = [
        {
            "header": "Model",
            "question": "Which model?",
            "options": [{"label": "Cells", "description": "In vitro"}],
        }
    ]
    text = (
        "Choose a model.\n"
        + json.dumps(questions)
        + '\nThe options are below.\n<run_spec>{"research_challenge":"Canonical",'
        '"questions":' + json.dumps(questions) + "}</run_spec>"
    )
    splitter = TurnSplitter()
    emitted = "".join(splitter.feed(character) for character in text)
    trailing, prose, fields = splitter.finish()
    assert '"header"' not in emitted + trailing
    assert prose == "Choose a model.\n\nThe options are below."
    assert fields is not None and fields["research_challenge"] == "Canonical"
    assert fields["questions"] == questions


@pytest.mark.parametrize(
    "text",
    [
        "A normal answer {with braces} stays visible.",
        'For example:\n{"dose": 10, "unit": "nM"}\nUse this format.',
        '```json\n{"dose": 10}\n```\nThat is an example.',
        '```python\nx = {"questions": []}\n```',
        'Incomplete example:\n{"dose": 10',
        "The formula is $x_{1}$.\n[Not JSON]",
    ],
)
def test_ordinary_prose_and_code_are_preserved(text: str) -> None:
    splitter = TurnSplitter()
    emitted = "".join(splitter.feed(character) for character in text)
    trailing, prose, fields = splitter.finish()
    assert emitted + trailing == text
    assert prose == text.strip()
    assert fields is None


def test_indented_metadata_is_withheld_across_whitespace_fragments() -> None:
    splitter = TurnSplitter()
    fragments = ["Read the explanation.\n", "  ", "{", '"questions": []', "}"]
    emitted = "".join(splitter.feed(fragment) for fragment in fragments)
    trailing, prose, fields = splitter.finish()
    assert '"questions"' not in emitted + trailing
    assert prose == "Read the explanation."
    assert fields == {"questions": []}


def test_json_envelope_exposes_only_its_assistant_message() -> None:
    splitter = TurnSplitter()
    envelope = json.dumps(
        {
            "assistant_message": "Which model should we use?",
            "questions": [],
            "research_challenge": "Study salt stress",
        }
    )
    emitted = "".join(splitter.feed(character) for character in envelope)
    trailing, prose, fields = splitter.finish()
    assert emitted + trailing == "Which model should we use?"
    assert prose == "Which model should we use?"
    assert fields is not None and fields["research_challenge"] == "Study salt stress"


def test_normal_prose_streams_before_the_rest_of_the_turn_arrives() -> None:
    splitter = TurnSplitter()
    assert splitter.feed("Here is the explanation. ") == "Here is the explanation. "
    assert splitter.feed("It remains incremental.\n") == "It remains incremental.\n"


def test_canonical_spec_inside_a_json_fence_leaves_no_empty_code_block() -> None:
    splitter = TurnSplitter()
    text = 'Which mechanism?\n```json\n<run_spec>{"questions": []}</run_spec>\n```'
    emitted = "".join(splitter.feed(character) for character in text)
    trailing, prose, fields = splitter.finish()
    assert emitted + trailing == "Which mechanism?\n"
    assert prose == "Which mechanism?"
    assert fields == {"questions": []}


@pytest.mark.parametrize("suffix", ['{"questions": [', '```json\n{"focus_area": ['])
def test_incomplete_control_payload_is_not_flushed_as_prose(suffix: str) -> None:
    splitter = TurnSplitter()
    text = "Which mechanism?\n" + suffix
    emitted = "".join(splitter.feed(character) for character in text)
    trailing, prose, fields = splitter.finish()
    assert emitted + trailing == "Which mechanism?\n"
    assert prose == "Which mechanism?"
    assert fields is None


def test_question_only_metadata_preserves_accumulated_interview_fields() -> None:
    from co_scientist.domains.chat.interviews.model import _turn_response

    interview = {
        "fields": {
            "research_challenge": "Study salt stress",
            "focus_area": ["Root growth"],
            "preferences": ["Human review"],
        }
    }
    response = _turn_response(interview, "Which mechanism?", {"questions": []})
    assert response["focus_area"] == ["Root growth"]
    assert response["preferences"] == ["Human review"]
    assert response["assistant_message"] == "Which mechanism?"


@pytest.mark.parametrize("fence", ["", "```json\n"])
def test_incomplete_question_array_is_not_flushed_as_prose(fence: str) -> None:
    splitter = TurnSplitter()
    text = "Choose a model.\n" + fence + '[{"header":"Model","question":"Which?","options":['
    emitted = "".join(splitter.feed(character) for character in text)
    trailing, prose, fields = splitter.finish()
    assert emitted + trailing == "Choose a model.\n"
    assert prose == "Choose a model."
    assert fields is None


def test_sse_exposes_prose_and_reasoning_but_never_question_json(
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
) -> None:
    from types import SimpleNamespace

    from co_scientist.main import app
    from fastapi.testclient import TestClient

    from ._interviews_helpers import _interview_payload, _start_interview
    from ._llm_fake_backend import install_completion_backend

    question = {
        "header": "Model",
        "question": "Which model?",
        "options": [
            {"label": "Cells", "description": "In vitro"},
            {"label": "Organoids", "description": "Tissue model"},
        ],
    }
    fields = {
        "research_challenge": "Study salt stress",
        "focus_area": [],
        "preferences": [],
        "lab_constraints": [],
        "completed": False,
        "questions": [question],
    }
    wire = (
        "Which model?\n"
        + json.dumps([question])
        + "\n<run_spec>"
        + json.dumps(fields)
        + "</run_spec>"
    )

    async def provider(**_kwargs: object) -> object:
        async def chunks() -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=None, reasoning_content="Compare models.")
                    )
                ]
            )
            for character in wire:
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=character, reasoning_content=None)
                        )
                    ]
                )

        return chunks()

    install_completion_backend(monkeypatch, provider)
    with TestClient(app) as client:
        response = _start_interview(client, {"X-Client-ID": "output-check"}, "Study salt stress")
    frames = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    prose = "".join(frame["content"] for frame in frames if frame["type"] == "chunk")
    assert '"question"' not in prose
    assert prose.strip() == "Which model?"
    assert any(frame == {"type": "reasoning", "content": "Compare models."} for frame in frames)
    assert _interview_payload(response)["turns"][-1]["questions"] == [
        {**question, "multi_select": False}
    ]


def test_single_question_object_is_hidden_and_recovered() -> None:
    question = {"header": "Model", "question": "Which model?", "options": []}
    splitter = TurnSplitter()
    emitted = "".join(
        splitter.feed(character) for character in "Choose a model.\n" + json.dumps(question)
    )
    trailing, prose, fields = splitter.finish()
    assert emitted + trailing == "Choose a model.\n"
    assert prose == "Choose a model."
    assert fields == {"questions": [question]}


def test_an_ordinary_question_and_answer_json_example_is_preserved() -> None:
    text = '{"question": "Is this valid?", "answer": "yes"}'
    splitter = TurnSplitter()
    emitted = "".join(splitter.feed(character) for character in text)
    trailing, prose, fields = splitter.finish()
    assert emitted + trailing == prose == text
    assert fields is None
