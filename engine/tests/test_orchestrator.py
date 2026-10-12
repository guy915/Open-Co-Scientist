from __future__ import annotations

import asyncio
from typing import Any

import pytest

from co_scientist.domains.research_state.models import ExecutionMetrics, Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.scheduling import SupervisorDecision, TaskType, owed_review
from co_scientist.science.scheduling.owed_review import (
    mark_owed_review_issued,
    owed_review_count,
    owed_review_issued,
    owed_review_targets,
)
from co_scientist.science.supervisor import orchestrator
from co_scientist.science.supervisor.orchestrator import orchestrator_node
from tests._state import make_hypothesis, make_review, make_state


def _hyp(hyp_id: str, wins: int = 0, losses: int = 0) -> Hypothesis:
    """Coverage is owed only to reviewed ideas; new entrants first owe a
    review."""
    return Hypothesis(
        id=hyp_id,
        text=f"statement {hyp_id}",
        win_count=wins,
        loss_count=losses,
        reviews=[make_review()],
    )


def _pool(unmatched: int, covered: int = 0) -> list[Hypothesis]:
    return [_hyp(f"u{i}") for i in range(unmatched)] + [
        _hyp(f"c{i}", wins=1, losses=1) for i in range(covered)
    ]


def _decide(
    pool: list[Hypothesis], book: dict[str, Any] | None = None, **fields: Any
) -> dict[str, Any]:
    """One orchestrator decision over a fully reviewed pool whose LLM call
    budget is spent, so only owed coverage can keep the run going."""
    state = make_state(
        hypotheses=pool,
        budget={"max_iterations": 5, "max_llm_calls": 10},
        metrics=ExecutionMetrics(llm_calls=999),
        orchestrator_state=book,
        current_iteration=0,
        **fields,
    )
    return asyncio.run(orchestrator_node(state))


def test_one_match_each_still_opens_a_settlement_episode() -> None:
    """A zero-match trigger misses pools where every idea still owes its
    second match."""
    delta = _decide([_hyp(f"h{i}", wins=1) for i in range(10)])

    assert delta["next_task"] == TaskType.RANK.value
    book = delta["orchestrator_state"]
    assert book["settlement_allowance"] == 4
    assert book["owed_at_last_settlement"] == 5


@pytest.mark.parametrize(
    ("pool_size", "never_helps", "rounds"),
    [(8, True, None), (2, False, 1)],
    ids=["ranking_never_helps", "distinct_pair_cap_binds"],
)
def test_settlement_is_a_finite_episode(
    pool_size: int, never_helps: bool, rounds: int | None
) -> None:
    """Ranking that never settles the debt, or settles it too slowly for the
    distinct-pair cap, must still end in a BUDGET termination."""
    book: dict[str, Any] | None = None
    backlog = pool_size
    allowances: list[int] = []
    ranked = 0

    for _ in range(50):
        delta = _decide(_pool(backlog, pool_size - backlog), book)
        if delta["termination_reason"]:
            break
        book = delta["orchestrator_state"]
        allowances.append(book["settlement_allowance"])
        ranked += 1
        if not never_helps:
            backlog -= 1

    assert delta["termination_reason"] == "budget"
    assert allowances == sorted(allowances, reverse=True)
    if rounds is not None:
        assert ranked == rounds
        assert backlog > 0


def test_a_late_backlog_settles_after_an_early_episode_cleared() -> None:
    delta = _decide(_pool(1, 5))
    assert delta["next_task"] == TaskType.RANK.value
    delta = _decide(_pool(0, 6), delta["orchestrator_state"])
    assert delta["orchestrator_state"]["settlement_allowance"] is None

    late = _decide(_pool(13, 35), delta["orchestrator_state"])

    assert late["next_task"] == TaskType.RANK.value


def test_an_under_covered_pool_does_not_rearm_the_allowance() -> None:
    """Closing an episode before all debt clears would rearm its allowance
    indefinitely."""
    pool = [_hyp(f"h{i}", wins=1) for i in range(10)]
    book = {
        "settlement_allowance": 2,
        "owed_at_last_settlement": 5,
        "pool_at_last_decision": 10,
    }
    delta = _decide(pool, book)
    assert delta["orchestrator_state"]["settlement_allowance"] == 2
    assert delta["orchestrator_state"]["owed_at_last_settlement"] == 5


_EXHAUSTED_TASKS = {"max_iterations": 5, "max_tasks": 3}


def _owed_state(hypothesis: Hypothesis, **fields: Any) -> WorkflowState:
    return make_state(
        hypotheses=[hypothesis],
        budget=_EXHAUSTED_TASKS,
        task_history=[{"task_type": "generate", "iteration": 0}] * 3,
        current_iteration=0,
        **fields,
    )


def test_orchestrator_forces_review_before_budget_can_terminate() -> None:
    hypothesis = make_hypothesis("newcomer")

    delta = asyncio.run(orchestrator_node(_owed_state(hypothesis)))

    assert delta["next_task"] == TaskType.REFLECT.value
    assert not delta.get("termination_reason")
    assert delta["hypotheses"] == [hypothesis]
    assert owed_review_issued(hypothesis)


@pytest.mark.parametrize("reviewed", [False, True])
def test_the_override_is_spent_once_so_the_next_cycle_terminates(
    reviewed: bool,
) -> None:
    """A failed review must not re-arm the override either."""
    hypothesis = make_hypothesis()
    asyncio.run(orchestrator_node(_owed_state(hypothesis)))
    if reviewed:
        hypothesis.reviews.append(make_review())

    delta = asyncio.run(orchestrator_node(_owed_state(hypothesis)))

    assert delta["termination_reason"] == "max_tasks"
    assert delta.get("hypotheses") in (None, [])


def test_the_override_never_fires_against_an_exhausted_llm_budget() -> None:
    hypothesis = make_hypothesis()
    state = make_state(
        hypotheses=[hypothesis],
        budget={"max_iterations": 5, "max_llm_calls": 10},
        metrics=ExecutionMetrics(llm_calls=10),
        current_iteration=0,
    )

    delta = asyncio.run(orchestrator_node(state))

    assert delta["termination_reason"] == "budget"
    assert not owed_review_issued(hypothesis)


def _state_with_steering(pending: bool) -> WorkflowState:
    hyps = [make_hypothesis(f"h{i}", elo_rating=1200) for i in range(4)]
    return make_state(
        hypotheses=hyps,
        pending_steering=pending,
        current_iteration=0,
    )


def test_pending_steering_schedules_generate_and_clears_flag() -> None:
    delta = asyncio.run(orchestrator_node(_state_with_steering(True)))
    assert delta["next_task"] == TaskType.GENERATE.value
    assert delta["pending_steering"] is False


def test_activity_uses_live_facts_not_planner_assertions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def _hallucinated_decision(
        *_args: object, **_kwargs: object
    ) -> tuple[SupervisorDecision, str, int]:
        return (
            SupervisorDecision(
                next_task=TaskType.GENERATE,
                reason="No hypotheses have been generated yet.",
                priority=80,
            ),
            "model",
            1,
        )

    monkeypatch.setattr(orchestrator, "choose_supervisor_task", _hallucinated_decision)
    state = _state_with_steering(False)
    delta = asyncio.run(orchestrator_node(state))

    message = str(delta["messages"])
    assert "No hypotheses" not in message
    assert "4 hypotheses" in message
    record = delta["task_history"][-1]
    assert record["reason"].startswith("Supervisor selected generate from live state")
    assert record["planner_reason"] == "No hypotheses have been generated yet."


def _unreviewed(index: int = 0) -> Hypothesis:
    return make_hypothesis(text=f"unreviewed idea {index}")


def _reviewed(index: int = 0) -> Hypothesis:
    return make_hypothesis(text=f"reviewed idea {index}", reviews=[make_review()])


def test_only_unreviewed_hypotheses_are_owed() -> None:
    unreviewed = _unreviewed(0)
    pool = [unreviewed, _reviewed(1)]

    assert owed_review_targets(pool) == [unreviewed]
    assert owed_review_count(pool) == 1


def test_an_unreviewed_but_already_archived_duplicate_is_not_owed() -> None:
    # Proximity can archive unreviewed ideas; a forced review of an archived
    # idea buys nothing.
    hypothesis = _unreviewed()
    hypothesis.review_disposition = "duplicate"

    assert owed_review_targets([hypothesis]) == []


def test_the_run_wide_ceiling_bounds_a_pathological_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(owed_review, "MAX_OWED_REVIEW_OVERRIDES_PER_RUN", 3)
    pool = [_unreviewed(i) for i in range(10)]

    first = owed_review_targets(pool)
    for hypothesis in first:
        mark_owed_review_issued(hypothesis)
    second = owed_review_targets(pool)

    assert len(first) == 3
    assert second == []


def test_a_marked_hypothesis_is_no_longer_owed_and_survives_a_checkpoint() -> None:
    hypothesis = _unreviewed()
    mark_owed_review_issued(hypothesis)

    restored = Hypothesis.from_dict(hypothesis.to_dict())

    assert owed_review_issued(hypothesis)
    assert not hypothesis.reviews
    assert owed_review_targets([hypothesis, restored]) == []
    assert owed_review_count([hypothesis]) == 0
