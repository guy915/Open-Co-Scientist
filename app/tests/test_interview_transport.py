from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from co_scientist.api.interviews import stream as interviews_stream
from co_scientist.core.config import settings
from co_scientist.domains.chat.interviews import model as interviews_model
from co_scientist.domains.chat.repository import interviews as store
from co_scientist.domains.chat.repository.interviews import NewInterviewTurn
from co_scientist.main import app
from fastapi.testclient import TestClient

from tests._llm_fake_backend import install_completion_backend

from ._interviews_helpers import (
    InterviewFields,
    _interview_payload,
    _patch_model_sequence,
    _response,
)

# Revised conversations invalidate every downstream turn derived from the
# withdrawn text.


HEADERS = {"X-Client-ID": "revision-scientist"}


def _start(client: TestClient, challenge: str) -> dict[str, Any]:
    return _interview_payload(
        client.post(
            "/api/interviews",
            headers=HEADERS,
            json={"research_challenge": challenge},
        )
    )


def _texts(interview: dict[str, Any]) -> list[str]:
    return [turn["content"] for turn in interview["turns"]]


def test_a_revision_re_derives_from_what_survives(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rewinding invalidates derived interview fields; carrying them forward
    # restores withdrawn conclusions.
    completed = InterviewFields(
        focus=["Efflux-pump regulation"],
        preferences=["Clinical isolates only"],
        completed=True,
    )
    _patch_model_sequence(
        monkeypatch,
        [_response("The goal is ready.", completed), _response("Next?")],
    )
    seen: list[dict[str, Any]] = []
    with TestClient(app) as client:
        started = _start(client, "How do bacteria regain susceptibility?")
        _capture_model_input(monkeypatch, seen, _response("Next?"))
        client.put(
            f"/api/interviews/{started['id']}/turns/{started['turns'][0]['id']}",
            headers=HEADERS,
            json={"content": "How do fungi regain susceptibility?"},
        )

    assert len(seen) == 1
    assert _texts(seen[0]) == ["How do fungi regain susceptibility?"]
    assert seen[0]["fields"] == {
        "research_challenge": "How do fungi regain susceptibility?",
        "focus_area": [],
        "preferences": [],
        "lab_constraints": [],
        "title": None,
    }


def _capture_model_input(
    monkeypatch: pytest.MonkeyPatch,
    seen: list[dict[str, Any]],
    reply: dict[str, Any],
) -> None:

    async def _model(
        interview: dict[str, Any],
        _on_reasoning: Any = None,
        _on_prose: Any = None,
    ) -> dict[str, Any]:
        seen.append(interview)
        return reply

    monkeypatch.setattr(interviews_model, "_call_interview_model", _model)


def test_a_revision_refuses_the_wrong_kind_of_turn_and_other_owners(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_model_sequence(monkeypatch, [_response("A first question.")])
    with TestClient(app) as client:
        started = _start(client, "How do bacteria regain susceptibility?")
        prompt, answer = started["turns"][0], started["turns"][-1]
        turns = f"/api/interviews/{started['id']}/turns"

        edit = client.put(
            f"{turns}/{answer['id']}",
            headers=HEADERS,
            json={"content": "Not a scientist turn."},
        )
        retry_prompt = client.post(f"{turns}/{prompt['id']}/retry", headers=HEADERS)
        missing = client.post(f"{turns}/99999/retry", headers=HEADERS)
        stranger = client.post(
            f"{turns}/{answer['id']}/retry",
            headers={"X-Client-ID": "someone-else"},
        )
        reread = client.get(f"/api/interviews/{started['id']}", headers=HEADERS)

    assert [
        edit.status_code,
        retry_prompt.status_code,
        missing.status_code,
        stranger.status_code,
    ] == [409, 409, 404, 404]
    assert len(reread.json()["turns"]) == 2


class _HangingStream:
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    def __aiter__(self) -> _HangingStream:
        return self

    async def __anext__(self) -> Any:
        self._started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


def _seed_interview(db_path: str) -> str:
    # Cancelled replies leave consecutive scientist turns because their messages
    # were persisted before streaming.
    interview = store.create_interview(
        "stop-scientist", "Restore antibiotic susceptibility", db_path=db_path
    )
    store.append_interview_turn(
        str(interview["id"]),
        NewInterviewTurn("user", "Prioritize efflux-pump regulation."),
        db_path=db_path,
    )
    return str(interview["id"])


async def test_cancel_mid_model_call_leaves_transcript_unchanged(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
) -> None:
    # Cancellation must propagate without creating a scripted fallback turn or
    # leaking the provider task.
    interview_id = _seed_interview(isolated_db)
    before = store.get_interview(interview_id, db_path=isolated_db)

    started = asyncio.Event()

    async def _fake_acompletion(**_kwargs: Any) -> Any:
        return _HangingStream(started)

    install_completion_backend(monkeypatch, _fake_acompletion)
    monkeypatch.setattr(settings, "chat_model_name", "deepseek/deepseek-chat")

    gen = interviews_stream._advance_stream(interview_id)
    consumer = asyncio.ensure_future(gen.__anext__())
    await asyncio.wait_for(started.wait(), timeout=5)

    # Client disconnect cancels the iteration task; aclose cannot run alongside
    # pending anext.
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer

    after = store.get_interview(interview_id, db_path=isolated_db)
    assert after is not None
    assert after == before
    assert [t["role"] for t in after["turns"]] == ["user", "user"]

    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    assert pending == []


def test_a_fields_edit_keeps_lab_constraints_it_does_not_send(
    isolated_db: str,
) -> None:
    created = store.create_interview(HEADERS["X-Client-ID"], "Map MCT1 in astrocytes")
    store.update_interview(
        created["id"],
        {**created["fields"], "lab_constraints": ["No animal work"]},
        "Which mechanism?",
    )
    edit = {
        "research_challenge": "Map MCT1 in astrocytes",
        "focus_area": ["Lactate shuttle"],
        "preferences": [],
    }
    with TestClient(app) as client:
        url = f"/api/interviews/{created['id']}/fields"
        kept = client.put(url, headers=HEADERS, json=edit)
        cleared = client.put(url, headers=HEADERS, json={**edit, "lab_constraints": []})

    assert kept.json()["fields"]["lab_constraints"] == ["No animal work"]
    assert kept.json()["fields"]["focus_area"] == ["Lactate shuttle"]
    assert cleared.json()["fields"]["lab_constraints"] == []
