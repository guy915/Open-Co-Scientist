from __future__ import annotations

from typing import Any, cast

import pytest

from co_scientist.domains.research_state.models import (
    ExecutionMetrics,
    Hypothesis,
    HypothesisReview,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.generation import generate as coordinator
from co_scientist.science.scheduling import Budget, SchedulerStats, TaskType


def make_article(title: str = "An article", **overrides: Any) -> Article:
    fields: dict[str, Any] = {"title": title}
    fields.update(overrides)
    return Article(**fields)


def make_hypothesis(text: str = "a hypothesis", **overrides: Any) -> Hypothesis:
    """Coverage is owed only to reviewed ideas; new entrants first owe a
    review."""
    fields: dict[str, Any] = {"text": text}
    fields.update(overrides)
    return Hypothesis(**fields)


def make_review(**overrides: Any) -> HypothesisReview:
    fields: dict[str, Any] = {
        "review_summary": "a solid review",
        "scores": {"novelty": 8, "relevance": 7},
        "safety_ethical_concerns": "none",
        "detailed_feedback": {"novelty": "novel angle"},
        "constructive_feedback": "tighten the experiment",
        "overall_score": 7.5,
    }
    fields.update(overrides)
    return HypothesisReview(**fields)


def make_generation_response(text: str, **overrides: Any) -> dict[str, Any]:
    entry = {
        "hypothesis": text,
        "explanation": "because the mechanism fits",
        "literature_grounding": None,
        "experiment": "run the assay",
        **overrides,
    }
    return {"hypotheses": [entry]}


def make_verification_response(**overrides: Any) -> dict[str, Any]:
    return {
        "probes": [
            {
                "question": "q",
                "answer": "a",
                "reasoning": "r",
                "assumption_is_fundamental": False,
            }
        ],
        "verdict": "holds",
        "overall_assessment": "ok",
        **overrides,
    }


def make_ranking_response(winner: str = "a", **overrides: Any) -> dict[str, Any]:
    return {
        "winner": winner,
        "decision_summary": "stub decision",
        "confidence_level": "High",
        **overrides,
    }


def make_allocation_response(task: str, reason: str, **overrides: Any) -> dict[str, Any]:
    return {"next_task": task, "reason": reason, **overrides}


def _run_and_pool_defaults() -> dict[str, Any]:
    return {
        "research_goal": "test research goal",
        "model_name": "test-model",
        "supervisor_model_name": "test-model",
        "max_iterations": 1,
        "initial_hypotheses_count": 2,
        "evolution_max_count": 2,
        "hypotheses": [],
        "current_iteration": 0,
        "supervisor_guidance": {},
        "meta_review": {},
        "research_overview": None,
        "removed_duplicates": [],
        "tournament_matchups": [],
        "evolution_details": [],
        "safety_decisions": [],
        "held_for_review": [],
        "metrics": ExecutionMetrics(),
        "start_time": 0.0,
        "run_id": "test-run",
        "progress_callback": None,
        "messages": [],
    }


def _input_and_literature_defaults() -> dict[str, Any]:
    return {
        "preferences": None,
        "attributes": None,
        "constraints": None,
        "lab_constraints": None,
        "starting_hypotheses": None,
        "literature": None,
        "articles_with_reasoning": None,
        "literature_review_queries": None,
        "articles": None,
        "debate_transcripts": None,
        "mcp_available": False,
        "pubmed_available": False,
        "enable_tool_calling_generation": False,
        "enable_overview_review": False,
        "enable_meta_review": True,
        "generation_strategy": "",
        "dev_test_lit_tools_isolation": False,
        "dev_mode": False,
        "tool_registry": None,
        "context_enrichment_sources": None,
    }


def make_state(**overrides: Any) -> WorkflowState:
    base: dict[str, Any] = {
        **_run_and_pool_defaults(),
        **_input_and_literature_defaults(),
    }
    base.update(overrides)
    return cast(WorkflowState, base)


class _ToolsRecorder:
    def __init__(self, hypotheses: list[Hypothesis], llm_calls: int = 0) -> None:
        self._hypotheses = hypotheses
        self.llm_calls = llm_calls
        self.called = False
        self.count: int | None = None

    async def __call__(
        self, _state: Any, count: int, _reference_index: Any
    ) -> tuple[list[Hypothesis], int]:
        self.called = True
        self.count = count
        return list(self._hypotheses), self.llm_calls


class _DebateRecorder:
    def __init__(
        self,
        hypotheses: list[Hypothesis],
        transcripts: list[dict[str, Any]],
        llm_calls: int = 0,
    ) -> None:
        self._hypotheses = hypotheses
        self._transcripts = transcripts
        self.llm_calls = llm_calls
        self.called = False
        self.count: int | None = None
        self.articles_with_reasoning: str | None = None

    async def __call__(
        self,
        *,
        state: Any,
        count: int,
        articles_with_reasoning: str | None = None,
        reference_index: Any = None,
    ) -> tuple[list[Hypothesis], list[dict[str, Any]], int]:
        self.called = True
        self.count = count
        self.articles_with_reasoning = articles_with_reasoning
        return list(self._hypotheses), list(self._transcripts), self.llm_calls


class _AssumptionsRecorder:
    def __init__(self, hypotheses: list[Hypothesis], llm_calls: int = 0) -> None:
        self._hypotheses = hypotheses
        self.llm_calls = llm_calls
        self.called = False
        self.count: int | None = None
        self.articles_with_reasoning: str | None = None
        self.reference_index: Any = None

    async def __call__(
        self,
        _state: Any,
        count: int,
        articles_with_reasoning: str | None = None,
        reference_index: Any = None,
    ) -> tuple[list[Hypothesis], int]:
        self.called = True
        self.count = count
        self.articles_with_reasoning = articles_with_reasoning
        self.reference_index = reference_index
        return list(self._hypotheses), self.llm_calls


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tools: _ToolsRecorder,
    debate: _DebateRecorder,
    assumptions: _AssumptionsRecorder | None = None,
) -> None:
    monkeypatch.setattr(coordinator, "generate_with_tools", tools)
    monkeypatch.setattr(coordinator, "generate_with_debate", debate)
    monkeypatch.setattr(
        coordinator,
        "generate_with_assumptions",
        assumptions or _AssumptionsRecorder([]),
    )


BUDGET = Budget(max_iterations=5, max_llm_calls=1000, max_tasks=100)


def healthy_stats(**overrides: object) -> SchedulerStats:
    base: dict[str, object] = {
        "pool_size": 6,
        "reviewed_count": 6,
        "unreviewed_count": 0,
        "rankable_count": 6,
        "match_coverage": 3.0,
        "iteration": 1,
        "rank_stable_cycles": 0,
    }
    base.update(overrides)
    return SchedulerStats(**base)  # type: ignore[arg-type]


def _stacked(*companions: str) -> list[dict[str, Any]]:
    return [{"action": "enqueue", "task_type": task, "reason": "stacked"} for task in companions]


def decision_states() -> list[WorkflowState]:
    meta, overview = TaskType.META_REVIEW.value, TaskType.SYNTHESIZE.value
    decisions: list[dict[str, Any]] = [{}, {"next_task": None}]
    decisions += [{"next_task": task.value} for task in TaskType]
    decisions.append({"next_task": "not_a_task"})
    stackings = [
        (TaskType.REFLECT, (meta,)),
        (TaskType.REFLECT, (overview,)),
        (TaskType.REFLECT, (meta, overview)),
        (TaskType.SYNTHESIZE, (meta,)),
    ]
    for primary, companions in stackings:
        decisions.append(
            {
                "next_task": primary.value,
                "supervisor_queue_actions": _stacked(*companions),
            }
        )
    return [make_state(**decision) for decision in decisions]
