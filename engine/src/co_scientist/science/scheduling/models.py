from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterable
from typing import Any


class TaskType(str, enum.Enum):
    GENERATE = "generate"
    REFLECT = "reflect"
    RANK = "rank"
    EVOLVE = "evolve"
    PROXIMITY = "proximity"
    META_REVIEW = "meta_review"
    SYNTHESIZE = "synthesize"
    TERMINATE = "terminate"


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    RETRIED = "retried"
    FAILED = "failed"


class TerminationReason(str, enum.Enum):
    """Cancellation belongs to durable dispatch; a scheduler stop would be
    weaker enforcement."""

    BUDGET = "budget"
    WALL_CLOCK = "wall_clock"
    MAX_TASKS = "max_tasks"
    MAX_IDEAS = "max_ideas"
    MAX_MATCHES_PER_IDEA = "max_matches_per_idea"
    COMPLETED = "completed"
    CONVERGED = "converged"
    SAFETY = "safety"


@dataclasses.dataclass(frozen=True)
class Budget:
    """max_matches_per_idea caps average coverage; it must exceed the
    minimum-coverage floor or the floor becomes unreachable."""

    max_iterations: int
    max_llm_calls: int | None = None
    max_tasks: int | None = None
    max_wall_clock_s: float | None = None
    max_ideas: int | None = None
    max_matches_per_idea: float | None = None
    finalists: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Budget:
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields})


@dataclasses.dataclass(frozen=True)
class SchedulerStats:
    pool_size: int = 0
    reviewed_count: int = 0
    unreviewed_count: int = 0

    # The per-idea marker is spent before outcome and also capped per run.
    owed_review_count: int = 0

    # Coverage over the whole pool would loop on evidence-rejected ideas.
    rankable_count: int = 0

    # Average coverage hides individual ideas that have never played.
    unmatched_rankable_count: int = 0

    # The same debt must open, size and close each settlement episode.
    owed_coverage_rounds: int = 0

    # Within an episode this only decreases; None is unissued, zero is spent.
    settlement_allowance: int | None = None

    # Stall detection saves cost; the allowance alone must prove termination.
    owed_at_last_settlement: int | None = None

    total_matches: int = 0
    # Average rather than minimum keeps coverage reachable by bounded
    # tournaments.
    match_coverage: float = 0.0

    # None means unbounded; a numeric default would silently disable ranking.
    tournament_rounds_remaining: int | None = None

    pool_grew_since_proximity: bool = False

    # Meta-review does not advance iterations; require work-cycle and new-
    # material clocks.
    iterations_since_meta_review: int = 0
    feedback_since_meta_review: int = 0

    # Disabling periodic cadence must not suppress the EVOLVE branch's meta-
    # review node.
    meta_review_enabled: bool = True

    # Overview is not a work task; its anchor resets on scheduling to prevent
    # loops.
    iterations_since_research_overview: int = 0

    top_elo: int = 0
    rank_stable_cycles: int = 0

    # Each stagnation episode earns an evolution attempt before convergence.
    evolved_since_stable: bool = False

    generation_yield: float = 0.0
    evolution_yield: float = 0.0

    iteration: int = 0

    # A standing stagnation condition would repeatedly evolve a converged pool.
    last_work_task: TaskType | None = None

    llm_calls: int = 0
    tasks_run: int = 0
    elapsed_s: float = 0.0

    pending_steering: bool = False

    # Durable dispatch enforces cancellation; this unwritten field is no second
    # stop gate.
    cancelled: bool = False
    safety_blocked: bool = False
    last_task_failed: TaskType | None = None
    retries_remaining: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        for key in ("last_task_failed", "last_work_task"):
            value = data.get(key)
            if isinstance(value, TaskType):
                data[key] = value.value
        return data


@dataclasses.dataclass(frozen=True)
class SupervisorDecision:
    next_task: TaskType
    reason: str
    priority: int = 50
    queue_actions: tuple[dict[str, Any], ...] = ()
    terminate: bool = False
    termination_reason: TerminationReason | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["next_task"] = self.next_task.value
        if self.termination_reason is not None:
            data["termination_reason"] = self.termination_reason.value
        return data


ENQUEUE_ACTION = "enqueue"


def stacked_task_values(
    queue_actions: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    """Durable routing and orchestrator bookkeeping must share one queue-
    action vocabulary."""
    return tuple(
        str(action["task_type"])
        for action in queue_actions
        if action.get("action") == ENQUEUE_ACTION and action.get("task_type")
    )


@dataclasses.dataclass
class TaskRecord:
    task_type: TaskType
    status: TaskStatus
    reason: str
    iteration: int
    termination_reason: TerminationReason | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["task_type"] = self.task_type.value
        data["status"] = self.status.value
        if self.termination_reason is not None:
            data["termination_reason"] = self.termination_reason.value
        return data
