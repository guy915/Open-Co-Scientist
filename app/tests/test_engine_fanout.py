from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import co_scientist.orchestration.engine_tasks.fanout as items
import pytest
from co_scientist.core.exceptions import (
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
)
from co_scientist.domains.research_state.models import Hypothesis, HypothesisReview
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.orchestration.checkpoint import restore_workflow_state
from co_scientist.orchestration.engine_tasks import fanout_aggregates as aggregates
from co_scientist.orchestration.engine_tasks import support as engine_tasks_support
from co_scientist.orchestration.engine_tasks.fanout import _mature_reflection_specs
from co_scientist.orchestration.engine_tasks.fanout_aggregates import _apply_review_items
from co_scientist.orchestration.engine_tasks.support import MATURE_REFLECTION_ITEM_TASK
from co_scientist.orchestration.repository import tasks as store
from co_scientist.orchestration.repository import tasks_lifecycle as lifecycle
from co_scientist.platform.db import checkpoints
from co_scientist.platform.db.models import ScientificTask
from co_scientist.platform.llm import ModelCallStats, record_call
from co_scientist.science import reflection as _operations_reflection

from tests._engine_tasks_helpers import (
    _Generator,
    _patch_generator,
    _patch_task_node,
    _seed_checkpoint,
    _task_events,
    _task_state,
)
from tests._store_helpers import enqueue_task, seed_run


async def _fake_review(**kwargs: Any) -> HypothesisReview:
    record_call(
        "fixture-model",
        ModelCallStats(calls=1, prompt_tokens=20, completion_tokens=10),
    )
    return HypothesisReview(
        review_summary=f"reviewed {kwargs['hypothesis_text']}",
        scores={"scientific_soundness": 8, "novelty": 8},
        safety_ethical_concerns="none",
        detailed_feedback={},
        constructive_feedback="continue",
        overall_score=8.0,
    )


async def _advance_to_review_parent(
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
    generator: _Generator,
    db_path: str,
) -> None:
    _patch_generator(monkeypatch, generator, restore=True, screen=True)
    engine_tasks.enqueue_bootstrap(run_id, db_path=db_path)
    await task_worker.run_once("bootstrap", run_id=run_id, db_path=db_path)
    supervisor = store.claim_task("supervisor", run_id=run_id, db_path=db_path)
    assert supervisor is not None

    async def supervisor_to_review(
        _name: str, current: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        return current, "review"

    _patch_task_node(monkeypatch, supervisor_to_review)
    result = await engine_tasks.execute_node_task(supervisor, db_path=db_path)
    assert lifecycle.complete_task(supervisor.id, "supervisor", result, db_path=db_path)
    review_parent = store.claim_task("parent", run_id=run_id, db_path=db_path)
    assert review_parent is not None
    parent_result = await engine_tasks.execute_node_task(review_parent, db_path=db_path)
    assert lifecycle.complete_task(review_parent.id, "parent", parent_result, db_path=db_path)


async def _fake_batch_review(hypotheses: list[Hypothesis], _context: Any) -> list[Any]:
    return [await _fake_review(hypothesis_text=h.text) for h in hypotheses]


async def _run_review_children_and_aggregate(run_id: str, db_path: str) -> None:
    first = store.claim_task("child-a", run_id=run_id, db_path=db_path)
    second = store.claim_task("child-b", run_id=run_id, db_path=db_path)
    assert first is not None and second is not None
    assert first.task_type == second.task_type == engine_tasks_support.REVIEW_ITEM_TASK
    assert [len(t.inputs["hypothesis_ids"]) for t in (first, second)] == [4, 1]
    first_result, second_result = await asyncio.gather(
        items.execute_review_item(first, db_path=db_path),
        items.execute_review_item(second, db_path=db_path),
    )
    assert lifecycle.complete_task(first.id, "child-a", first_result, db_path=db_path)
    assert lifecycle.complete_task(second.id, "child-b", second_result, db_path=db_path)
    aggregate = store.claim_task("aggregate", run_id=run_id, db_path=db_path)
    assert aggregate is not None
    aggregate_result = await aggregates.execute_review_aggregate(aggregate, db_path=db_path)
    assert aggregate_result["successful_reviews"] == 5
    assert lifecycle.complete_task(aggregate.id, "aggregate", aggregate_result, db_path=db_path)


@pytest.mark.asyncio
async def test_review_fanout_uses_independent_leases_and_one_aggregate_commit(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("Task-level science")
    state = _task_state(run.id)
    state["hypotheses"] = [
        Hypothesis(text=text) for text in ("alpha", "beta", "gamma", "delta", "epsilon")
    ]
    await _advance_to_review_parent(run.id, monkeypatch, _Generator(state), isolated_db)

    import co_scientist.science.reflection.review as review_module

    monkeypatch.setattr(review_module, "review_single_hypothesis", _fake_review)
    monkeypatch.setattr(review_module, "review_comparative_batch", _fake_batch_review)
    await _run_review_children_and_aggregate(run.id, isolated_db)

    checkpoint = checkpoints.get_latest_checkpoint(run.id, db_path=isolated_db)
    assert checkpoint is not None and checkpoint["seq"] == 3
    persisted = checkpoint["state"]["state"]["hypotheses"]
    assert [hypothesis["score"] for hypothesis in persisted] == [8.0] * 5
    review_events = _task_events(run.id, "review", db_path=isolated_db)
    assert len(review_events) == 1
    assert review_events[0]["payload"]["successor"] == "comprehensive_reflection"
    usage = restore_workflow_state(checkpoint["state"])["metrics"].model_usage
    assert (
        usage["review::fixture-model"]
        == ModelCallStats(calls=5, prompt_tokens=100, completion_tokens=50).as_dict()
    )


@pytest.mark.asyncio
async def test_review_aggregate_is_ready_after_isolated_child_failure(
    isolated_db: str,
) -> None:
    run = seed_run("Task-level science")
    failed = enqueue_task(
        run.id,
        engine_tasks_support.REVIEW_ITEM_TASK,
        "failed-child",
        max_attempts=1,
        db_path=isolated_db,
    )
    aggregate = enqueue_task(
        run.id,
        engine_tasks_support.REVIEW_AGGREGATE_TASK,
        "aggregate",
        dependencies=(failed.id,),
        provenance={"allow_failed_dependencies": True},
        db_path=isolated_db,
    )
    leased = store.claim_task("child", run_id=run.id, db_path=isolated_db)
    assert leased is not None and leased.id == failed.id
    assert store.fail_task(
        leased.id,
        "child",
        "provider failed",
        retryable=False,
        db_path=isolated_db,
    )

    ready = store.claim_task("aggregate", run_id=run.id, db_path=isolated_db)
    assert ready is not None and ready.id == aggregate.id


# Preserve rate-park and call-budget exception types through review handlers to
# worker outcome policy.


PARK = LLMRateLimitParkError(1788825600.0, "message_per_day")
OVER_BUDGET = LLMCallBudgetExceededError(2501, 2500)


def _seed_mature_review_item(
    run_id: str, monkeypatch: pytest.MonkeyPatch, db_path: str
) -> ScientificTask:
    state = _task_state(run_id)
    hypothesis = Hypothesis(text="a mechanism worth reviewing")
    hypothesis.review_disposition = "viable"
    state["hypotheses"] = [hypothesis]
    checkpoint_seq = _seed_checkpoint(run_id, state, db_path=db_path)
    enqueue_task(
        run_id,
        MATURE_REFLECTION_ITEM_TASK,
        "control-flow-item",
        inputs={
            "checkpoint_seq": checkpoint_seq,
            "hypothesis_id": hypothesis.id,
            "review_mode": "full",
        },
        db_path=db_path,
    )
    _patch_generator(monkeypatch, _Generator(state), restore=True)
    leased = store.claim_task("item", run_id=run_id, db_path=db_path)
    assert leased is not None
    return leased


def _install_failing_review(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    # Inject failures inside the review provider seam; replacing the whole
    # handler would hide swallowed parks.
    import co_scientist.science.reflection.comprehensive_reflection as comp
    from co_scientist.science.reflection.review_evidence import _ReviewEvidence

    async def _evidence(*_: Any, **__: Any) -> _ReviewEvidence:
        return _ReviewEvidence([], [], [], None)

    async def _observations(*_: Any, **__: Any) -> str | None:
        return None

    async def _call(*_: Any, **__: Any) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(comp, "_review_evidence_for", _evidence)
    monkeypatch.setattr(comp, "_observations_for", _observations)
    monkeypatch.setattr(comp, "call_llm_json", _call)


@pytest.mark.parametrize("error", [PARK, OVER_BUDGET])
async def test_a_control_flow_error_leaves_the_item_unchanged(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    # Workers dispatch by exception type; wrapping parks or budget errors as
    # RuntimeError changes retry outcomes.
    run = seed_run("Task-level science", profile="extended")
    leased = _seed_mature_review_item(run.id, monkeypatch, isolated_db)
    _install_failing_review(monkeypatch, error)

    with pytest.raises(type(error)):
        await items.execute_mature_reflection_item(leased, db_path=isolated_db)


async def test_an_ordinary_provider_failure_is_still_a_retryable_failure(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_run("Task-level science", profile="extended")
    leased = _seed_mature_review_item(run.id, monkeypatch, isolated_db)
    _install_failing_review(monkeypatch, ValueError("unparseable answer"))

    with pytest.raises(RuntimeError):
        await items.execute_mature_reflection_item(leased, db_path=isolated_db)


@pytest.mark.parametrize("error", [PARK, OVER_BUDGET])
def test_a_control_flow_error_escapes_the_ranking_wave(
    error: Exception,
) -> None:
    # gather must propagate parks or waves commit incomplete rounds against an
    # unchanged provider cap.
    from co_scientist.orchestration.engine_tasks.ranking import _surviving_judgements
    from co_scientist.science.ranking import RankingJudgement

    verdict = RankingJudgement("a", {"decision_summary": "A is stronger"}, 2)

    with pytest.raises(type(error)):
        _surviving_judgements(["pair-0", "pair-1"], [verdict, error])


def _dispositions_blocking_review() -> HypothesisReview:
    return HypothesisReview(
        review_summary="unsound",
        scores={"scientific_soundness": 2, "novelty": 8},
        safety_ethical_concerns="none",
        detailed_feedback={},
        constructive_feedback="rework",
        overall_score=3.0,
    )


def _dispositions_blocked_hypothesis() -> Hypothesis:
    hypothesis = Hypothesis(text="alpha", reviews=[_dispositions_blocking_review()])
    hypothesis.review_disposition = "inaccurate"
    return hypothesis


def test_aggregate_reopens_an_idea_a_deeper_review_cleared() -> None:
    hypothesis = _dispositions_blocked_hypothesis()
    hypothesis.enrichments["full"] = {"verdict": "sound"}

    applied = _apply_review_items({hypothesis.id: hypothesis}, [], None)

    assert (applied.successful, applied.failed) == (0, 0)
    assert hypothesis.is_rankable()


def test_aggregate_leaves_the_evidence_gate_s_disposition_alone() -> None:
    # The evidence gate owns displaced dispositions; re-deriving
    # evidence_blocked elsewhere strands recovery.
    hypothesis = _dispositions_blocked_hypothesis()
    hypothesis.review_disposition = "evidence_blocked"
    hypothesis.enrichments["full"] = {"verdict": "sound"}

    _apply_review_items({hypothesis.id: hypothesis}, [], None)

    assert hypothesis.review_disposition == "evidence_blocked"


def _restore_item(monkeypatch: pytest.MonkeyPatch, mode: str = "full") -> Any:
    idea = Hypothesis(id="idea", text="Scientific claim")
    state = {"hypotheses": [idea], "articles_with_reasoning": "Literature"}
    monkeypatch.setattr(items, "_restore_item_checkpoint", lambda *args, **kwargs: (state, 7))
    return SimpleNamespace(inputs={"hypothesis_id": idea.id, "review_mode": mode})


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PARK, OVER_BUDGET])
async def test_verification_item_control_errors_reach_worker(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    task = _restore_item(monkeypatch)
    monkeypatch.setattr(
        _operations_reflection,
        "verify_hypothesis",
        AsyncMock(side_effect=error),
    )
    with pytest.raises(type(error)) as caught:
        await items.execute_verification_item(task)
    assert caught.value is error


@pytest.mark.asyncio
async def test_verification_none_becomes_durable_item_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _restore_item(monkeypatch)
    monkeypatch.setattr(
        _operations_reflection,
        "verify_hypothesis",
        AsyncMock(return_value=None),
    )
    with pytest.raises(RuntimeError, match="deep verification failed for idea"):
        await items.execute_verification_item(task)


@pytest.mark.asyncio
async def test_observation_rejects_missing_literature_before_call(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = seed_run("Observation grounding", profile="extended")
    state = _task_state(run.id)
    idea = Hypothesis(id="observation-idea", text="Scientific claim")
    state.update(hypotheses=[idea], articles_with_reasoning="")
    seq = _seed_checkpoint(run.id, state, db_path=isolated_db)
    task = enqueue_task(
        run.id,
        MATURE_REFLECTION_ITEM_TASK,
        "observation-without-literature",
        inputs={
            "hypothesis_id": idea.id,
            "review_mode": "observation",
            "checkpoint_seq": seq,
        },
        db_path=isolated_db,
    )
    _patch_generator(monkeypatch, _Generator(state), restore=True)
    operation = AsyncMock()
    monkeypatch.setattr(_operations_reflection, "observe_hypothesis", operation)
    with pytest.raises(RuntimeError, match="no literature context"):
        await items.execute_mature_reflection_item(task, db_path=isolated_db)
    operation.assert_not_awaited()


# Blocked ideas get one recurrent recheck; record attempts even on failure so
# later cycles cannot refund it.


def _recheck_blocked_hypothesis(text: str = "alpha") -> Hypothesis:
    hypothesis = Hypothesis(text=text, reviews=[_dispositions_blocking_review()])
    hypothesis.review_disposition = "inaccurate"
    return hypothesis


def _state(hypotheses: list[Hypothesis]) -> dict[str, Any]:
    return {"hypotheses": hypotheses, "current_iteration": 0}


@pytest.mark.parametrize("recheck", [True, False])
def test_a_failed_item_records_a_recheck_attempt_only_for_rechecks(
    monkeypatch: pytest.MonkeyPatch, recheck: bool
) -> None:
    hypothesis = _recheck_blocked_hypothesis()
    _patch_items(
        monkeypatch,
        inputs={
            "hypothesis_id": hypothesis.id,
            "recheck": recheck,
            "review_mode": "recurrent" if recheck else "full",
        },
        status="failed",
    )

    aggregates._apply_mature_reflection_items({hypothesis.id: hypothesis}, ["item-1"], 0, None)

    owed = _mature_reflection_specs(_state([hypothesis]))
    assert len(owed) == (0 if recheck else 1)
    if recheck:
        assert hypothesis.enrichments["recurrent"]["verdict"] == "unreviewed"


def _patch_items(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[str, dict[str, Any]] | None = None,
    *,
    inputs: dict[str, Any] | None = None,
    status: str = "completed",
) -> None:

    class _Item:
        def __init__(self, payload: dict[str, Any] | None) -> None:
            self.inputs = inputs or {}
            self.status = status
            self.error = "acceptance unknown"
            self.result = payload

    monkeypatch.setattr(
        aggregates,
        "_require_item_task",
        lambda item_id, db_path, kind="": _Item((results or {}).get(str(item_id))),
    )


def _mature_item(
    hypothesis: Hypothesis, mode: str, review: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return {
        "hypothesis_id": hypothesis.id,
        "review_mode": mode,
        "review": review,
        **extra,
    }


def test_mature_reflection_aggregate_applies_fatal_dispositions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hypothesis = Hypothesis(text="idea")
    hypothesis.review_disposition = "viable"
    _patch_items(
        monkeypatch,
        {
            "item-full": _mature_item(
                hypothesis,
                "full",
                {"verdict": "rejected", "justification": "circular"},
            ),
            "item-simulation": _mature_item(hypothesis, "simulation", {"verdict": "holds"}),
        },
    )

    items = aggregates._apply_mature_reflection_items(
        {hypothesis.id: hypothesis},
        ["item-full", "item-simulation"],
        current_iteration=1,
        db_path=None,
    )

    assert items.successful == 2
    assert hypothesis.enrichments["full"]["verdict"] == "rejected"
    assert hypothesis.review_disposition == "inaccurate"
    assert not hypothesis.is_rankable()


@pytest.mark.parametrize(
    ("criteria", "disposition"),
    [(["Discriminating experimental design"], "inaccurate"), (None, "viable")],
)
def test_review_aggregate_gates_on_the_run_criteria_when_it_has_any(
    monkeypatch: pytest.MonkeyPatch,
    criteria: list[str] | None,
    disposition: str,
) -> None:
    hypothesis = Hypothesis(text="idea")
    review = HypothesisReview(
        review_summary="summary",
        scores={"scientific_soundness": 9, "novelty": 9, "testability": 1},
        safety_ethical_concerns="none",
        detailed_feedback={},
        constructive_feedback="feedback",
        overall_score=6.0,
    )
    _patch_items(
        monkeypatch,
        {
            "item-review": {
                "hypothesis_id": hypothesis.id,
                "review": dataclasses.asdict(review),
            }
        },
    )

    applied = aggregates._apply_review_items(
        {hypothesis.id: hypothesis},
        ["item-review"],
        db_path=None,
        criteria=criteria,
    )

    assert (applied.successful, applied.failed) == (1, 0)
    assert hypothesis.review_disposition == disposition
    assert hypothesis.is_rankable() is (disposition == "viable")


@pytest.mark.parametrize("ledger", [{"goal": "reverse fibrosis", "calls": []}, None])
def test_the_aggregate_carries_each_items_research_to_the_run_once(
    monkeypatch: pytest.MonkeyPatch, ledger: dict[str, Any] | None
) -> None:
    # Retrieval ledgers belong to the run and must survive discarded item
    # results without duplicate searches.
    hypothesis = Hypothesis(text="idea")
    hypothesis.review_disposition = "viable"
    _patch_items(
        monkeypatch,
        {
            "item-full": _mature_item(
                hypothesis, "full", {"verdict": "sound"}, research_ledger=ledger
            ),
            "item-simulation": _mature_item(
                hypothesis,
                "simulation",
                {"verdict": "holds"},
                research_ledger=dict(ledger) if ledger else None,
            ),
        },
    )

    items = aggregates._apply_mature_reflection_items(
        {hypothesis.id: hypothesis},
        ["item-full", "item-simulation"],
        current_iteration=1,
        db_path=None,
    )
    update = aggregates._mature_reflection_update(
        {"hypotheses": [hypothesis], "articles": []}, items
    )

    assert update["research_ledgers"] == ([ledger] if ledger else [])


def _screening_review() -> dict[str, Any]:
    return dataclasses.asdict(
        HypothesisReview(
            review_summary="summary",
            scores={"scientific_soundness": 8, "novelty": 8},
            safety_ethical_concerns="none",
            detailed_feedback={},
            constructive_feedback="feedback",
            overall_score=8.0,
        )
    )


def test_a_batch_missing_one_answer_fails_only_that_idea(monkeypatch: pytest.MonkeyPatch) -> None:
    answered, missing = Hypothesis(text="answered"), Hypothesis(text="missing")
    _patch_items(
        monkeypatch,
        {
            "batch": {
                "reviews": [
                    {"hypothesis_id": answered.id, "review": _screening_review()},
                    {"hypothesis_id": missing.id, "review": None},
                ]
            }
        },
    )

    applied = aggregates._apply_review_items(
        {answered.id: answered, missing.id: missing}, ["batch"], db_path=None
    )

    assert (applied.successful, applied.failed, applied.llm_calls) == (1, 1, 1)
    assert answered.score == 8.0
    assert missing.review_disposition == "review_failed"


def test_a_failed_batch_leaves_each_of_its_ideas_for_a_later_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [Hypothesis(text=text) for text in ("a", "b", "c")]
    _patch_items(monkeypatch, inputs={"hypothesis_ids": [h.id for h in pool]}, status="failed")

    applied = aggregates._apply_review_items({h.id: h for h in pool}, ["batch"], db_path=None)

    assert (applied.successful, applied.failed) == (0, 3)
    assert {h.review_disposition for h in pool} == {"review_failed"}
