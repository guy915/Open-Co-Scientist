from __future__ import annotations

import dataclasses
import enum
import json
import sqlite3
from dataclasses import dataclass
from typing import Any


class RunStatus(str, enum.Enum):
    """Lifecycle states of a run, persisted in the runs.status column."""

    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED = "blocked"
    PAUSED = "paused"


_ACTIVE_RUN_STATUSES: tuple[str, str, str] = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.SYNTHESIZING.value,
)

TERMINAL_STATUSES: tuple[RunStatus, ...] = (
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.BLOCKED,
    RunStatus.CANCELLED,
)

DEMO_CLIENT_ID = "__demo__"


@dataclass
class RunRow:
    id: str
    research_goal: str
    profile: str
    status: str
    provider: str
    config: dict[str, Any]
    client_id: str
    created_at: float
    updated_at: float
    completed_at: float | None
    error: str | None
    title: str | None = None
    # List queries populate aggregate Elo; single-run reads and runs without
    # hypotheses retain None.
    top_elo: int | None = None
    # An empty listed hypothesis set is []; None denotes a single-run read
    # without this enrichment.
    top_hypotheses: list[str] | None = None
    latest_stage: str | None = None
    # Legacy backend NULL falls back to provider provenance, never current
    # process configuration.
    llm_backend: str | None = None
    # Restatement is report-only and omitted from run-list JSON; legacy and
    # offline rows legitimately leave it NULL.
    goal_restatement: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "research_goal": self.research_goal,
            "title": self.title,
            # Keep profile alongside run_mode for clients still reading the
            # legacy key.
            "run_mode": self.profile,
            "profile": self.profile,
            "status": self.status,
            "provider": self.provider,
            "config": self.config,
            "is_demo": self.client_id == DEMO_CLIENT_ID,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "top_elo": self.top_elo,
            "top_hypotheses": self.top_hypotheses,
            "latest_stage": self.latest_stage,
            "llm_backend": self.llm_backend,
        }


@dataclass
class MessageRow:
    """Legacy rows can lack acknowledgement fields; applied_decision records
    the scheduling consequence without event-time correlation.
    """

    id: int
    run_id: str
    sender: str
    content: str
    kind: str
    created_at: float
    applied: bool
    meta: dict[str, Any] | None = None
    applied_at: float | None = None
    applied_decision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def row_to_run(row: sqlite3.Row) -> RunRow:
    keys = row.keys()
    return RunRow(
        id=row["id"],
        research_goal=row["research_goal"],
        profile=row["profile"],
        status=row["status"],
        provider=row["provider"],
        config=json.loads(row["config_json"]),
        client_id=row["client_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        error=row["error"],
        title=row["title"] if "title" in keys else None,
        top_elo=row["top_elo"] if "top_elo" in keys else None,
        llm_backend=row["llm_backend"] if "llm_backend" in keys else None,
        goal_restatement=(row["goal_restatement"] if "goal_restatement" in keys else None),
    )


def _parse_message_meta(row: sqlite3.Row) -> dict[str, Any] | None:
    raw = row["meta_json"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def row_to_message(row: sqlite3.Row) -> MessageRow:
    keys = row.keys()
    return MessageRow(
        id=row["id"],
        run_id=row["run_id"],
        sender=row["sender"],
        content=row["content"],
        kind=row["kind"],
        created_at=row["created_at"],
        applied=bool(row["applied"]),
        meta=_parse_message_meta(row),
        applied_at=row["applied_at"] if "applied_at" in keys else None,
        applied_decision=(row["applied_decision"] if "applied_decision" in keys else None),
    )


UNKNOWN_PROVIDER_OUTCOME_ERROR = (
    "The provider may have accepted the request; acceptance and any charge "
    "are unconfirmed. Automatic replay was stopped."
)


@dataclasses.dataclass(frozen=True)
class TaskFailure:
    error: str
    failure_kind: str | None = None


@dataclasses.dataclass(frozen=True)
class ScientificTask:
    id: str
    run_id: str
    task_type: str
    status: str
    priority: int
    inputs: dict[str, Any]
    dependencies: tuple[str, ...]
    provenance: dict[str, Any]
    idempotency_key: str
    budget: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: float | None
    result: dict[str, Any] | None
    error: str | None
    created_at: float
    updated_at: float
    started_at: float | None
    completed_at: float | None
    # Rows predating attempt history decode as the empty sequence, not a
    # fabricated failure.
    attempts: tuple[dict[str, Any], ...] = ()
    attempt_started_at: float | None = None
    available_at: float | None = None


def row_to_task(row: sqlite3.Row) -> ScientificTask:
    return ScientificTask(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        task_type=str(row["task_type"]),
        status=str(row["status"]),
        priority=int(row["priority"]),
        inputs=json.loads(row["inputs_json"]),
        dependencies=tuple(json.loads(row["dependencies_json"])),
        provenance=json.loads(row["provenance_json"]),
        idempotency_key=str(row["idempotency_key"]),
        budget=json.loads(row["budget_json"]),
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        attempts=tuple(json.loads(row["attempts_json"])),
        attempt_started_at=row["attempt_started_at"],
        available_at=row["available_at"],
    )
