from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, with_config
from typing_extensions import NotRequired, TypedDict


@with_config(ConfigDict(extra="allow"))
class InterviewFields(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    research_challenge: str
    focus_area: list[str]
    preferences: list[str]
    lab_constraints: list[str]
    title: str | None


@with_config(ConfigDict(extra="allow"))
class InterviewQuestionOption(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    label: str
    description: str


@with_config(ConfigDict(extra="allow"))
class InterviewQuestion(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    header: str
    question: str
    multi_select: bool
    options: list[InterviewQuestionOption]


@with_config(ConfigDict(extra="allow"))
class InterviewTurn(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: int
    role: Literal["user"] | Literal["agent"]
    content: str
    reasoning: str | None
    fallback: bool
    questions: list[InterviewQuestion]
    created_at: float


@with_config(ConfigDict(extra="allow"))
class ChatSummary(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    title: str | None
    challenge: str
    status: Literal["active"] | Literal["completed"] | Literal["cancelled"]
    run_id: str | None
    created_at: float
    updated_at: float


@with_config(ConfigDict(extra="allow"))
class Interview(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    client_id: str
    status: Literal["active"] | Literal["completed"] | Literal["cancelled"]
    fields: InterviewFields
    current_question: str | None
    turns: list[InterviewTurn]
    documents: NotRequired[list[InterviewDocument]]
    created_at: float
    updated_at: float
    completed_at: float | None
    run_id: NotRequired[str | None]


@with_config(ConfigDict(extra="allow"))
class InterviewDocument(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    title: str
    mime_type: str
    byte_size: int
