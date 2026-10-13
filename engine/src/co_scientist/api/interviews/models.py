from __future__ import annotations

from pydantic import BaseModel, Field


class CreateInterviewRequest(BaseModel):
    """Initial scientist challenge for a new interview.

    ``document_ids`` names documents already staged through
    ``/api/documents``. They are attached to the interview, so the very
    first turn is scoped with the scientist's own material rather than
    reaching the work only after the plan is fixed.
    """

    research_challenge: str = Field(..., min_length=1, max_length=20_000)
    document_ids: list[str] = Field(default_factory=list)


class InterviewTurnRequest(BaseModel):
    """One scientist answer or correction, with any newly attached documents."""

    content: str = Field(..., min_length=1, max_length=20_000)
    document_ids: list[str] = Field(default_factory=list)


class InterviewFieldsRequest(BaseModel):
    """Scientist-authored edits to the five structured fields.

    Omitted ``title`` and ``lab_constraints`` keep their stored values, so a
    client that does not edit them cannot erase them.
    """

    research_challenge: str = Field(..., min_length=1, max_length=20_000)
    focus_area: list[str]
    preferences: list[str]
    lab_constraints: list[str] = Field(default_factory=list)
    title: str | None = Field(None, max_length=200)
