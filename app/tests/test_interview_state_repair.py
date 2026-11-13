from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from co_scientist.domains.chat.interviews import model as interviews_model
from co_scientist.domains.chat.interviews.questions import normalized_questions
from co_scientist.domains.chat.interviews.state_repair import _repair_prompt, repair_turn_state

from ._interviews_helpers import _fake_stream
from ._llm_fake_backend import completion_response, install_completion_backend

_INTERVIEW: dict[str, Any] = {
    "turns": [
        {"role": "user", "content": "Reverse cardiac fibrosis"},
        {"role": "agent", "content": "Which mechanism?"},
        {"role": "user", "content": "Focus on TGF-beta signalling in fibroblasts"},
    ],
    "fields": {
        "research_challenge": "Reverse cardiac fibrosis",
        "focus_area": [],
        "preferences": [],
        "lab_constraints": [],
        "title": None,
    },
}

_REPAIRED_STATE = {
    "research_challenge": "Reverse cardiac fibrosis",
    "focus_area": ["TGF-beta signalling in fibroblasts"],
    "preferences": [],
    "lab_constraints": [],
    "title": "",
    "completed": False,
    "questions": [
        {
            "header": "Model system",
            "question": "Which model system should we use?",
            "multi_select": False,
            "options": [
                {
                    "label": "Primary cells",
                    "description": "Closest to patients",
                    "recommended": True,
                },
                {
                    "label": "Mouse model",
                    "description": "Whole-organ context",
                    "recommended": False,
                },
            ],
        }
    ],
}

# The model answers in prose and stops without the trailing state block.
_TURN_WITHOUT_BLOCK = "Noted: TGF-beta in fibroblasts. Which model system should we use?"


def _provider(state: dict[str, Any] | None, requests: list[dict[str, Any]]) -> Any:
    async def respond(**kwargs: Any) -> Any:
        requests.append(kwargs)
        if kwargs.get("stream"):
            return _fake_stream(_TURN_WITHOUT_BLOCK)
        if state is None:
            raise RuntimeError("provider down")
        return completion_response(json.dumps(state))

    return respond


async def test_a_turn_without_a_state_block_recovers_its_fields_and_questions(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    requests: list[dict[str, Any]] = []
    install_completion_backend(monkeypatch, _provider(_REPAIRED_STATE, requests))

    result = await interviews_model._call_interview_model(_INTERVIEW)

    assert result["assistant_message"] == _TURN_WITHOUT_BLOCK
    assert result["focus_area"] == ["TGF-beta signalling in fibroblasts"]
    assert result["title"] is None
    assert result["completed"] is False
    assert normalized_questions(result["questions"]) == _REPAIRED_STATE["questions"]
    assert [bool(request.get("stream")) for request in requests] == [True, False]


async def test_a_failed_state_repair_keeps_the_earlier_fields(
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_completion_backend(monkeypatch, _provider(None, []))

    with caplog.at_level(logging.WARNING):
        result = await interviews_model._call_interview_model(_INTERVIEW)

    assert result["focus_area"] == []
    assert result["completed"] is False
    assert "no usable spec block" in caplog.text


async def test_state_repair_makes_no_outbound_request_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []
    install_completion_backend(monkeypatch, _provider(_REPAIRED_STATE, requests))
    monkeypatch.setenv("COSCIENTIST_FORCE_OFFLINE", "1")

    assert await repair_turn_state(_INTERVIEW, _TURN_WITHOUT_BLOCK) is None
    assert requests == []


def test_state_repair_prompt_keeps_fixed_no_invention_instructions_first() -> None:
    first = _repair_prompt(_INTERVIEW, "Which assay?")
    second = _repair_prompt(_INTERVIEW, "Rescue or depletion?")
    assert first[: first.run_end] == second[: second.run_end]
    assert "Never invent" in second[: second.run_end]
    assert "Rescue or depletion?" in second[second.run_end : second.item_end]
    assert "TGF-beta signalling" in second[second.run_end : second.item_end]


def test_a_repair_that_finds_no_question_is_not_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    empty = {"header": "", "question": "", "multi_select": False, "options": []}
    with caplog.at_level(logging.WARNING):
        assert normalized_questions([empty]) == []
    assert "none usable" not in caplog.text


def test_an_offered_question_without_options_still_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lone = {"header": "", "question": "Which one?", "multi_select": False, "options": []}
    with caplog.at_level(logging.WARNING):
        assert normalized_questions([lone]) == []
    assert "offered 1 question(s), none usable" in caplog.text


def _options(*flags: bool | None) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for index, flag in enumerate(flags):
        option: dict[str, Any] = {"label": f"Option {index}", "description": ""}
        if flag is not None:
            option["recommended"] = flag
        options.append(option)
    return options


def _recommended(question: dict[str, Any]) -> list[str]:
    return [option["label"] for option in question["options"] if option["recommended"]]


def test_a_question_offers_at_most_four_options_and_keeps_the_recommended_one() -> None:
    raw = {"question": "Which?", "options": _options(False, False, False, False, False, True)}

    (question,) = normalized_questions([raw])

    labels = [option["label"] for option in question["options"]]
    assert labels == ["Option 0", "Option 1", "Option 2", "Option 5"]
    assert _recommended(question) == ["Option 5"]


def test_a_single_select_question_recommends_exactly_one_option() -> None:
    unflagged = {"question": "Which?", "options": _options(None, None, None)}
    doubled = {"question": "Which two?", "options": _options(False, True, True)}

    first, second = normalized_questions([unflagged, doubled])

    assert _recommended(first) == ["Option 0"]
    assert _recommended(second) == ["Option 1"]


def test_a_multi_select_question_may_recommend_several_options() -> None:
    raw = {"question": "Which?", "multi_select": True, "options": _options(True, False, True)}

    (question,) = normalized_questions([raw])

    assert _recommended(question) == ["Option 0", "Option 2"]


def test_a_turn_offers_at_most_three_questions() -> None:
    raw = [{"question": f"Q{index}?", "options": _options(None, None)} for index in range(5)]

    assert [question["question"] for question in normalized_questions(raw)] == ["Q0?", "Q1?", "Q2?"]
