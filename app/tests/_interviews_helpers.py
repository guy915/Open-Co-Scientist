from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from co_scientist.domains.chat.interviews import model as interviews_model
from co_scientist.domains.chat.interviews.model import (
    CLOSE_MARKER,
    OPEN_MARKER,
    InterviewModelUnavailableError,
)
from fastapi.testclient import TestClient

from ._llm_fake_backend import install_completion_backend


@dataclasses.dataclass(frozen=True)
class InterviewFields:
    challenge: str = "How can resistant bacteria regain drug susceptibility?"
    focus: list[str] | None = None
    preferences: list[str] | None = None
    title: str | None = None
    completed: bool = False


def _response(message: str, fields: InterviewFields | None = None) -> dict[str, Any]:
    fields = fields or InterviewFields()
    return {
        "assistant_message": message,
        "research_challenge": fields.challenge,
        "focus_area": fields.focus or [],
        "preferences": fields.preferences or [],
        "title": fields.title,
        "completed": fields.completed,
    }


def _wire_turn(response: dict[str, Any]) -> str:
    fields = {key: value for key, value in response.items() if key != "assistant_message"}
    return f"{response['assistant_message']}\n\n{OPEN_MARKER}\n{json.dumps(fields)}\n{CLOSE_MARKER}"


def _interview_payload(response: Any) -> dict[str, Any]:
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[len("data: ") :])
        if event["type"] == "interview":
            return cast(dict[str, Any], event["interview"])
    raise AssertionError(f"no interview frame in stream: {response.text!r}")


def _start_interview(
    client: TestClient,
    headers: dict[str, str],
    challenge: str = "Study resistance",
) -> Any:
    return client.post(
        "/api/interviews",
        headers=headers,
        json={"research_challenge": challenge},
    )


def _send_turn(client: TestClient, headers: dict[str, str], interview_id: str, content: str) -> Any:
    return client.post(
        f"/api/interviews/{interview_id}/turns",
        headers=headers,
        json={"content": content},
    )


def _patch_model_sequence(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]) -> None:
    async def auxiliary_failure(**_kwargs: Any) -> Any:
        raise RuntimeError("unmodeled auxiliary call in interview fixture")

    install_completion_backend(monkeypatch, auxiliary_failure)

    # The fake accepts the prose-sink argument so it cannot fabricate a provider
    # failure.
    replies = iter(responses)

    async def _model(
        _interview: dict[str, Any],
        _on_reasoning: Any = None,
        _on_prose: Any = None,
    ) -> dict[str, Any]:
        return next(replies)

    monkeypatch.setattr(interviews_model, "_call_interview_model", _model)


def _patch_model_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model_failing_after(monkeypatch, [])


def _patch_model_failing_after(
    monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]
) -> None:
    replies: Iterator[dict[str, Any]] = iter(responses)

    async def _model(
        _interview: dict[str, Any],
        _on_reasoning: Any = None,
        _on_prose: Any = None,
    ) -> dict[str, Any]:
        try:
            return next(replies)
        except StopIteration:
            raise InterviewModelUnavailableError("unavailable") from None

    monkeypatch.setattr(interviews_model, "_call_interview_model", _model)


def _antibiotic_responses() -> list[dict[str, Any]]:
    return [
        _response("Which resistance mechanism should the study prioritize?"),
        _response(
            "What models, constraints, or exclusions should guide it?",
            InterviewFields(focus=["Efflux-pump regulation"]),
        ),
        _response(
            "The goal is ready for run configuration.",
            InterviewFields(
                focus=["Efflux-pump regulation"],
                preferences=[
                    "Use clinical Gram-negative isolates",
                    "Exclude new antibiotic discovery",
                ],
                title="Restoring Antibiotic Susceptibility",
                completed=True,
            ),
        ),
    ]


def _run_antibiotic_interview(
    client: TestClient, headers: dict[str, str]
) -> tuple[str, Any, Any, Any]:
    created = client.post(
        "/api/interviews",
        headers=headers,
        json={"research_challenge": ("How can resistant bacteria regain drug susceptibility?")},
    )
    interview_id = _interview_payload(created)["id"]
    second = client.post(
        f"/api/interviews/{interview_id}/turns",
        headers=headers,
        json={"content": "Prioritize efflux-pump regulation."},
    )
    final = client.post(
        f"/api/interviews/{interview_id}/turns",
        headers=headers,
        json={
            "content": ("Use clinical Gram-negative isolates and exclude new antibiotic discovery.")
        },
    )
    return interview_id, created, second, final


def _fake_stream(content: str, reasoning: str = "") -> Any:
    # DeepSeek can emit reasoning before content.

    def _chunk(
        *, reasoning_content: str | None = None, content: str | None = None
    ) -> SimpleNamespace:
        delta = SimpleNamespace(reasoning_content=reasoning_content, content=content)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    async def _chunks() -> Any:
        if reasoning:
            yield _chunk(reasoning_content=reasoning)
        yield _chunk(content=content)

    return _chunks()
