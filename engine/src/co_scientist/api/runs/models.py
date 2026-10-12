from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from co_scientist.core.constants import (
    MAX_PLANNING_IDEAS,
    MAX_PLANNING_ITERATIONS,
    MAX_PLANNING_K_FACTOR,
)
from co_scientist.core.run_modes import (
    RUN_FOCUS_PATTERN,
    RUN_TIER_PATTERN,
    PlanningLists,
    normalize_run_focus,
    normalize_run_tier,
    resolved_run_config,
    setup_config,
)


class CreateRunRequest(BaseModel):
    """Body for POST /api/runs; everything but the goal is optional."""

    research_goal: str = Field(..., min_length=1, max_length=32_000)
    interview_id: str | None = Field(None, max_length=128)
    # Staged and interview documents join the run atomically so private
    # grounding cannot
    # fail in a second write.
    document_ids: list[str] = Field(default_factory=list, max_length=8)
    requirements: list[str] | None = Field(None, max_length=64)
    # Attribute axes accept legacy prose and structured scale or categorical
    # forms.
    attributes: list[str | dict[str, Any]] | None = Field(None, max_length=64)
    # Criteria accept legacy prose and structured name/value pairs.
    criteria: list[str | dict[str, str]] | None = Field(None, max_length=64)
    focus: str | None = Field(None, pattern=RUN_FOCUS_PATTERN)
    tier: str | None = Field(None, pattern=RUN_TIER_PATTERN)
    initial_hypotheses_count: int | None = Field(None, ge=0, le=MAX_PLANNING_IDEAS)
    max_iterations: int | None = Field(None, ge=0, le=MAX_PLANNING_ITERATIONS)
    evolution_max_count: int | None = Field(None, ge=0, le=MAX_PLANNING_IDEAS)
    k_factor: int | None = Field(None, gt=0, le=MAX_PLANNING_K_FACTOR)
    enable_literature_review: bool | None = None
    enable_web_search: bool | None = None
    completion_email: str | None = Field(
        None,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    notify_on_completion: bool = False

    @model_validator(mode="after")
    def bound_planning_bytes(self) -> CreateRunRequest:
        resolved_run_config(
            self.model_dump(
                include={
                    "tier",
                    "initial_hypotheses_count",
                    "max_iterations",
                    "evolution_max_count",
                    "k_factor",
                },
                exclude_none=True,
            )
        )
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode()) > 64_000:
            raise ValueError("run planning exceeds the 64000 byte limit")
        return self


class ResumeRunRequest(BaseModel):
    acknowledge_unknown_outcome: bool = False


class StartRunRequest(BaseModel):
    """Body for POST /api/runs/{id}/start.

    Empty by design (the mock provider option was removed): the engine is
    the only provider now. Kept as a distinct model, rather than dropping
    the request body entirely, so the endpoint keeps accepting the ``{}``
    body existing clients already send.
    """


class RenameRunRequest(BaseModel):
    """Body for PATCH /api/runs/{id} (rename).

    ``max_length`` matches ``co_scientist.domains.chat.goal_text._MAX_TITLE_CHARS``, the ceiling
    a generated title is held to, so a hand-written one cannot outgrow the
    surfaces (sidebar rows, run titlebar, report header) built for it.
    """

    title: str = Field(..., min_length=1, max_length=80)


class SendMessageRequest(BaseModel):
    """Body for POST /api/runs/{id}/messages (steering)."""

    content: str = Field(..., min_length=1, max_length=16_000)


class AskRequest(BaseModel):
    """Body for POST /api/runs/{id}/messages/ask (Q&A)."""

    question: str = Field(..., min_length=1, max_length=16_000)


class QaRevisionRequest(BaseModel):
    question: str | None = Field(default=None, min_length=1, max_length=16_000)


class StartAnnouncementRequest(BaseModel):
    """Body for POST /api/runs/{id}/messages/started (session announcement).

    ``prompt`` is the scientist's own start request, sent verbatim so it is
    persisted and replayed as the turn it is rather than reconstructed
    server-side from a button press.
    """

    prompt: str = Field(..., min_length=1, max_length=16_000)


# Attachments are inert text, not executable or extracted archives; the cap
# bounds
# storage and prompt growth.
MAX_ATTACHMENT_CHARS = 200_000


class HumanAttachmentRequest(BaseModel):
    """Body for POST /api/runs/{id}/attachments (scientist-provided corpus).

    Text-only by design: the endpoint accepts a document's plain text, never a
    binary or archive, so there is no extraction/malware surface. ``consent``
    must be true — the scientist explicitly consents to indexing the document
    into the run's private retrieval corpus.
    """

    title: str = Field(..., min_length=1, max_length=512)
    text: str = Field(..., min_length=1, max_length=MAX_ATTACHMENT_CHARS)
    consent: bool = False


class SafetyAdjudicationRequest(BaseModel):
    """Human resolution of a safety item held for review."""

    resolution: Literal["approved", "rejected"]


def _run_overrides_from_request(
    req: CreateRunRequest, *, focus: str, tier: str, setup: dict[str, Any]
) -> dict[str, Any]:
    """Absent numeric knobs retain tier defaults; only explicitly supplied
    values override them.
    """
    overrides: dict[str, Any] = {
        "tier": tier,
        "focus": focus,
        "setup": setup,
    }
    numeric_overrides: tuple[tuple[str, Any], ...] = (
        ("initial_hypotheses_count", req.initial_hypotheses_count),
        ("max_iterations", req.max_iterations),
        ("evolution_max_count", req.evolution_max_count),
        ("k_factor", req.k_factor),
        ("enable_literature_review", req.enable_literature_review),
        ("enable_web_search", req.enable_web_search),
    )
    for key, value in numeric_overrides:
        if value is not None:
            overrides[key] = value
    return overrides


def _build_create_run_config(
    req: CreateRunRequest,
) -> tuple[dict[str, Any], str, str]:
    focus = normalize_run_focus(req.focus)
    tier = normalize_run_tier(req.tier)
    setup = setup_config(
        research_goal=req.research_goal,
        lists=PlanningLists(
            requirements=req.requirements,
            attributes=req.attributes,
            criteria=req.criteria,
        ),
        focus=focus,
        tier=tier,
    )
    overrides = _run_overrides_from_request(req, focus=focus, tier=tier, setup=setup)
    return resolved_run_config(overrides), focus, tier
