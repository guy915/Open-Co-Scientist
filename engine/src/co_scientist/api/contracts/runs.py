from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, with_config
from typing_extensions import NotRequired, TypedDict

from co_scientist.api.contracts.common import (
    RunConfig,
    RunMode,
    RunStatus,
)


@with_config(ConfigDict(extra="allow"))
class RunExecutionProgress(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    determinate: bool
    completed_tasks: int
    total_tasks: int
    fraction: float | None
    active_task: str | None
    queued_tasks: int


@with_config(ConfigDict(extra="allow"))
class Run(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    research_goal: str
    title: NotRequired[str | None]
    run_mode: NotRequired[RunMode]
    profile: RunMode
    status: RunStatus
    provider: Literal["mock"] | Literal["engine"]
    config: RunConfig
    is_demo: NotRequired[bool]
    llm_backend: NotRequired[str | None]
    created_at: float
    updated_at: float
    completed_at: float | None
    error: str | None
    top_elo: NotRequired[float | None]
    top_hypotheses: NotRequired[list[str] | None]
    latest_stage: NotRequired[str | None]
    awaiting_decision_count: NotRequired[int]
    execution_progress: NotRequired[RunExecutionProgress]


@with_config(ConfigDict(extra="allow"))
class RunSummary(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    events: int
    hypotheses: int
    evidence: int
    matches: int
    reviews: int


@with_config(ConfigDict(extra="allow"))
class RunWithSummary(Run):
    """JSON contract; omitted fields stay omitted."""

    summary: RunSummary
    failure_kind: NotRequired[str | None]
    can_resume: NotRequired[bool]
    resume_requires_ack: NotRequired[bool]


@with_config(ConfigDict(extra="allow"))
class QaSource(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    n: int
    evidence_id: str
    title: str
    url: NotRequired[str | None]
    source: NotRequired[str | None]
    year: NotRequired[float | None]
    state: str
    passage: NotRequired[str | None]


@with_config(ConfigDict(extra="allow"))
class MessageMetadata(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    sources: NotRequired[list[QaSource]]
    reasoning: NotRequired[str]
    fallback: NotRequired[bool]


@with_config(ConfigDict(extra="allow"))
class RunMessage(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: int
    run_id: str
    sender: str
    content: str
    kind: str
    created_at: float
    applied: bool
    meta: MessageMetadata | None
    applied_at: NotRequired[float | None]
    applied_decision: NotRequired[str | None]
