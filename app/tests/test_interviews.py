from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import co_scientist.domains.chat.interviews.questions as question_repair
import pytest
from co_scientist.core.config import settings
from co_scientist.domains.chat.interviews import model as interviews_model
from co_scientist.domains.chat.repository import interviews as store
from co_scientist.domains.chat.repository.interviews import NewInterviewTurn
from co_scientist.main import app
from co_scientist.platform.llm import process_mode
from fastapi.testclient import TestClient

from tests._client import create_run as _create_run
from tests._llm_fake_backend import install_completion_backend

from ._client import make_client
from ._interviews_helpers import (
    _antibiotic_responses,
    _fake_stream,
    _interview_payload,
    _patch_model_raising,
    _patch_model_sequence,
    _response,
    _run_antibiotic_interview,
    _send_turn,
    _start_interview,
    _wire_turn,
)

_OWNER = {"X-Client-ID": "chat-owner"}


def _make_chat(owner: str = "chat-owner", turns: int = 2) -> str:
    interview = store.create_interview(owner, "Why do biofilms resist drugs?")
    interview_id: str = str(interview["id"])
    for index in range(turns):
        store.append_interview_turn(interview_id, NewInterviewTurn("user", f"answer {index}"))
    return interview_id


def test_deleting_a_chat_removes_it_and_its_transcript() -> None:
    client: TestClient = make_client()
    kept, removed = _make_chat(), _make_chat()

    response = client.delete(f"/api/interviews/{removed}", headers=_OWNER)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["counts"]["interviews"] == 1
    assert body["counts"]["interview_turns"] >= 2
    assert store.get_interview(removed) is None
    ids = {chat["id"] for chat in client.get("/api/interviews", headers=_OWNER).json()}
    assert kept in ids
    assert removed not in ids


@pytest.mark.parametrize(
    ("owner", "caller"),
    [
        ("chat-owner", "someone-else"),
        ("", ""),
    ],
    ids=["another-client", "identity-less"],
)
def test_only_the_owner_can_delete_a_chat(owner: str, caller: str) -> None:
    client: TestClient = make_client()
    chat_id = _make_chat(owner=owner)

    response = client.delete(f"/api/interviews/{chat_id}", headers={"X-Client-ID": caller})

    assert response.status_code == 404
    assert store.get_interview(chat_id) is not None
    missing = client.delete("/api/interviews/no-such-chat", headers=_OWNER)
    assert missing.status_code == 404


# Option repair must not cost the scientist an otherwise valid prose turn.

_QUESTIONS = [
    {
        "header": "Model system",
        "question": "Which model system should the ideas be built around?",
        "multi_select": False,
        "options": [
            {"label": "Primary human cells", "description": "Closest to"},
            {"label": "iPSC-derived line", "description": "Renewable"},
        ],
    }
]


def _turn_offering(questions: Any) -> str:
    response = _response("Which model system should we build around?")
    return _wire_turn({**response, "questions": questions})


def _created_turn(
    monkeypatch: pytest.MonkeyPatch,
    turn: str,
    repaired: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    async def _fake_acompletion(**_kwargs: Any) -> Any:
        return _fake_stream(turn)

    install_completion_backend(monkeypatch, _fake_acompletion)
    if repaired is not None:

        async def _fake_repair(_message: str) -> list[dict[str, Any]]:
            return repaired

        monkeypatch.setattr(question_repair, "repair_questions", _fake_repair)
    with TestClient(app) as client:
        created = _start_interview(
            client, {"X-Client-ID": "scientist-a"}, "Reverse cardiac fibrosis"
        )
    agent_turn: dict[str, Any] = _interview_payload(created)["turns"][-1]
    return agent_turn


def test_a_streamed_turn_carries_its_questions_without_leaking_the_block(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    turn = _created_turn(monkeypatch, _turn_offering(_QUESTIONS), repaired=[])

    assert turn["questions"] == _QUESTIONS
    assert turn["content"] == "Which model system should we build around?"


def test_question_repair_shares_the_interview_turn_budget(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    monkeypatch.setattr(settings, "app_llm_max_calls", 1)

    async def provider(**kwargs: Any) -> Any:
        assert kwargs.get("stream"), "repair must not dispatch over the cap"
        return _fake_stream(_wire_turn(_response("Which one?")))

    fake = install_completion_backend(monkeypatch, provider)
    with TestClient(app) as client:
        created = _start_interview(
            client, {"X-Client-ID": "scientist-a"}, "Reverse cardiac fibrosis"
        )
    turn = _interview_payload(created)["turns"][-1]
    assert turn["content"] == "Which one?"
    assert turn["questions"] == []
    assert len(fake.requests) == 1


# Resolve started-run links per client so reopened chats cannot start duplicate
# or foreign runs.


def test_a_completed_interview_seeds_its_run_and_links_the_chat_to_it(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_model_sequence(monkeypatch, _antibiotic_responses())
    headers = {"X-Client-ID": "scientist-a"}
    with TestClient(app) as client:
        interview_id, created, second, final = _run_antibiotic_interview(client, headers)
        assert created.status_code == 200
        assert _interview_payload(second)["status"] == "active"
        payload = _interview_payload(final)
        assert payload["status"] == "completed"
        assert set(payload["fields"]) == {
            "research_challenge",
            "focus_area",
            "preferences",
            "lab_constraints",
            "title",
        }
        assert [turn["role"] for turn in payload["turns"]] == [
            "user",
            "agent",
            "user",
            "agent",
            "user",
            "agent",
        ]
        before = client.get("/api/interviews", headers=headers).json()
        run = _create_run(
            client,
            "client placeholder is not authoritative",
            headers=headers,
            interview_id=interview_id,
            tier="ultra",
        )
        after = client.get("/api/interviews", headers=headers).json()
        reopened = client.get(f"/api/interviews/{interview_id}", headers=headers).json()
        other = client.get("/api/interviews", headers={"X-Client-ID": "scientist-b"}).json()

    run_payload = run.json()
    assert run.status_code == 200
    assert run_payload["research_goal"] == payload["fields"]["research_challenge"]
    assert run_payload["title"] == "Restoring Antibiotic Susceptibility"
    assert run_payload["config"]["interview_id"] == interview_id
    assert [chat["id"] for chat in before] == [interview_id]
    assert before[0]["run_id"] is None
    assert before[0]["title"] == "Restoring Antibiotic Susceptibility"
    assert after[0]["run_id"] == run_payload["id"]
    assert reopened["fields"] == payload["fields"]
    assert reopened["run_id"] == run_payload["id"]
    assert other == []


def test_interview_is_owner_scoped_and_requires_completion(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_model_sequence(monkeypatch, [_response("Which mechanism should be prioritized?")])
    with TestClient(app) as client:
        created = _start_interview(client, {"X-Client-ID": "owner"}, "Study resistance")
        interview_id = _interview_payload(created)["id"]
        denied = client.get(
            f"/api/interviews/{interview_id}",
            headers={"X-Client-ID": "other"},
        )
        premature = _create_run(
            client,
            "Study resistance",
            headers={"X-Client-ID": "owner"},
            interview_id=interview_id,
        )
    assert denied.status_code == 404
    assert premature.status_code == 409


def _flags(interview: dict[str, Any], role: str) -> list[bool]:
    return [bool(turn["fallback"]) for turn in interview["turns"] if turn["role"] == role]


def test_interview_stays_usable_and_marks_fallback_turns_during_model_outage(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_model_raising(monkeypatch)
    headers = {"X-Client-ID": "offline-scientist"}
    with TestClient(app) as client:
        created = _start_interview(client, headers, "Test astrocyte lactate transport")
        interview_id = _interview_payload(created)["id"]
        focused = _send_turn(
            client,
            headers,
            interview_id,
            "Prioritize MCT1 and MCT4 mechanisms.",
        )
        completed = _send_turn(
            client,
            headers,
            interview_id,
            "Use human organoids and exclude animal work.",
        )

    assert created.status_code == 200
    assert _flags(_interview_payload(created), "agent") == [True]
    assert _flags(_interview_payload(focused), "agent") == [True, True]
    assert _flags(_interview_payload(focused), "user") == [False, False]
    assert _interview_payload(focused)["fields"]["focus_area"] == [
        "Prioritize MCT1 and MCT4 mechanisms."
    ]
    payload = _interview_payload(completed)
    assert payload["status"] == "completed"
    assert payload["fields"] == {
        "research_challenge": "Test astrocyte lactate transport",
        "focus_area": ["Prioritize MCT1 and MCT4 mechanisms."],
        "preferences": ["Use human organoids and exclude animal work."],
        "lab_constraints": [],
        "title": None,
    }


def test_production_interview_outage_keeps_fields_and_does_not_complete(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_model_raising(monkeypatch)
    monkeypatch.delenv("COSCIENTIST_TEST_DOUBLE", raising=False)
    monkeypatch.setenv("COSCIENTIST_FORCE_OFFLINE", "1")
    previous = process_mode.install(process_mode.EnvProcessMode())
    interview_id = _make_chat("owner", turns=3)
    before = store.get_interview(interview_id)
    assert before is not None
    try:
        with TestClient(app) as client:
            reply = _send_turn(client, {"X-Client-ID": "owner"}, interview_id, "Use organoids")
    finally:
        process_mode.install(previous)
    assert '"type": "error"' in reply.text
    assert "No model is available right now" in reply.text
    after = store.get_interview(interview_id)
    assert after is not None
    assert after["fields"] == before["fields"]
    assert after["status"] == before["status"]
    assert not any(turn["role"] == "agent" for turn in after["turns"])


def _reasoning_only_stream(reasoning: str) -> Any:
    # Thinking-only streams never emit content, unlike an empty content chunk.
    def _chunk(reasoning_content: str) -> SimpleNamespace:
        delta = SimpleNamespace(reasoning_content=reasoning_content, content=None)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    async def _chunks() -> Any:
        yield _chunk(reasoning)

    return _chunks()


async def test_thinking_only_turn_retries_once_with_thinking_off(
    monkeypatch: pytest.MonkeyPatch, reachable_provider: None
) -> None:
    calls: list[dict[str, Any]] = []
    streams = [
        _reasoning_only_stream("brainstorming dozens of candidate drugs..."),
        _fake_stream(_wire_turn(_response("Which mechanism?"))),
    ]

    async def _fake_acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return streams[len(calls) - 1]

    install_completion_backend(monkeypatch, _fake_acompletion)
    monkeypatch.setattr(settings, "chat_model_name", "deepseek/deepseek-chat")

    reasoning_fragments: list[str] = []

    async def _on_reasoning(fragment: str) -> None:
        reasoning_fragments.append(fragment)

    interview = {
        "turns": [{"role": "user", "content": "restore susceptibility"}],
        "fields": {},
    }
    result = await interviews_model._call_interview_model(interview, on_reasoning=_on_reasoning)

    assert len(calls) == 2
    # High is DeepSeek's lowest reasoning tier.
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[1]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in calls[1]
    assert any("retrying" in fragment for fragment in reasoning_fragments)
    assert result["assistant_message"] == "Which mechanism?"
