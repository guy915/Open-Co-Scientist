from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from co_scientist.orchestration.engine_tasks.support import (
    NodeCompletion,
    TaskCommit,
    _emit_node_completion,
    _require_item_task,
    _save_state_and_enqueue,
    leased_state,
    merge_usage_snapshots,
)
from co_scientist.orchestration.repository import tasks
from co_scientist.orchestration.repository.tasks import NewTask
from co_scientist.platform.db.models import ScientificTask

if TYPE_CHECKING:
    from co_scientist.domains.research_state.state import WorkflowState


@dataclass(frozen=True)
class _AppliedItems:
    successful: int
    failed: int
    llm_calls: int
    results: list[dict[str, Any]] = field(default_factory=list)
    model_usage: dict[str, dict[str, Any]] = field(default_factory=dict)
    research_ledgers: list[dict[str, Any]] = field(default_factory=list)


async def _checkpoint_and_advance(
    commit: TaskCommit,
    committed: dict[str, Any],
    node_name: str,
) -> tuple[int, str | None]:
    """Aggregate successors come from the engine's route table so durable
    execution follows routing changes.
    """
    from co_scientist.orchestration.task_runtime import next_task_type

    successor = next_task_type(node_name, cast("WorkflowState", committed))
    checkpoint_seq, successor_id = _save_state_and_enqueue(commit, committed, successor)
    await _emit_node_completion(
        commit.task.run_id,
        NodeCompletion(node_name, successor, checkpoint_seq),
        committed,
        commit.db_path,
    )
    return checkpoint_seq, successor_id


def _apply_one_reflection_item(
    hypothesis: Any,
    mode: Any,
    review: dict[str, Any],
    current_iteration: int,
) -> None:
    from co_scientist.science.reflection.comprehensive_reflection import (
        store_failed_finalist_review,
        store_finalist_review,
    )
    from co_scientist.science.reflection.reflection import (
        apply_observation_result,
    )
    from co_scientist.science.reflection.review_gate import (
        ReviewType,
        store_mature_review_result,
    )

    if mode is ReviewType.FINALIST:
        if review.get("verdict") == "unreviewed":
            store_failed_finalist_review(hypothesis, review.get("justification"), current_iteration)
        else:
            store_finalist_review(hypothesis, review, current_iteration)
        return
    if mode is ReviewType.OBSERVATION:
        # Shared engine application carries confirmed strengths into notes on
        # both execution paths.
        apply_observation_result(hypothesis, review)
        return
    # Shared engine application keeps mature-review dispositions identical
    # across execution paths.
    store_mature_review_result(hypothesis, mode, review, current_iteration)


def _mark_recheck_item(by_id: dict[str, Any], item: Any) -> None:
    """A failed item still spends its one recheck, preventing repeated waves
    against the same failure.
    """
    from co_scientist.science.reflection.review_gate import mark_recheck_issued

    if not item.inputs.get("recheck"):
        return
    hypothesis = by_id.get(str(item.inputs.get("hypothesis_id") or ""))
    if hypothesis is not None:
        mark_recheck_issued(hypothesis)


def _apply_mature_reflection_items(
    by_id: dict[str, Any],
    item_task_ids: Sequence[Any],
    current_iteration: int,
    db_path: str | None,
) -> _AppliedItems:
    from co_scientist.science.reflection.review_gate import ReviewType

    successful = 0
    failed = 0
    reflection_results: list[dict[str, Any]] = []
    usage_snapshots: list[dict[str, Any]] = []
    ledgers: list[dict[str, Any]] = []
    for item_id in item_task_ids:
        item = _require_item_task(item_id, db_path, kind="reflection task")
        _mark_recheck_item(by_id, item)
        if item.status != "completed" or not item.result:
            failed += 1
            hypothesis = by_id.get(str(item.inputs.get("hypothesis_id") or ""))
            if hypothesis is not None:
                _apply_one_reflection_item(
                    hypothesis,
                    ReviewType(str(item.inputs["review_mode"])),
                    {"verdict": "unreviewed", "justification": item.error},
                    current_iteration,
                )
            continue
        hypothesis = by_id[str(item.result["hypothesis_id"])]
        mode = ReviewType(str(item.result["review_mode"]))
        review = item.result["review"]
        reflection_results.append(review)
        _apply_one_reflection_item(hypothesis, mode, review, current_iteration)
        usage_snapshots.append(item.result.get("model_usage") or {})
        ledger = item.result.get("research_ledger")
        if isinstance(ledger, dict) and ledger and ledger not in ledgers:
            ledgers.append(ledger)
        successful += 1
    return _AppliedItems(
        successful=successful,
        failed=failed,
        llm_calls=successful + failed,
        results=reflection_results,
        model_usage=merge_usage_snapshots(usage_snapshots),
        research_ledgers=ledgers,
    )


def _mature_reflection_update(
    state: dict[str, Any],
    items: _AppliedItems,
) -> dict[str, Any]:
    from co_scientist.domains.research_state.models import (
        MetricDeltas,
        create_metrics_update,
        phase_message,
    )
    from co_scientist.science.reflection.deep_verification import (
        merge_retrieved_articles,
    )

    state["articles"] = merge_retrieved_articles(
        state.get("articles"), cast("list[dict[str, Any] | None]", items.results)
    )
    return {
        "hypotheses": state["hypotheses"],
        "articles": state["articles"],
        # The state reducer appends searches instead of replacing earlier
        # literature provenance.
        "research_ledgers": items.research_ledgers,
        "metrics": create_metrics_update(
            deltas=MetricDeltas(llm_calls=items.llm_calls),
            model_usage=items.model_usage,
        ),
        "messages": phase_message(
            "reflection",
            f"Completed {items.successful} mature reviews; {items.failed} isolated failures",
        ),
    }


async def execute_mature_reflection_aggregate(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    replay, state, current_seq = leased_state(task, db_path, label="reflection aggregate")
    if replay is not None:
        return replay
    by_id = {hypothesis.id: hypothesis for hypothesis in state["hypotheses"]}
    items = _apply_mature_reflection_items(
        by_id,
        task.inputs.get("item_task_ids", []),
        int(state.get("current_iteration", 0)),
        db_path,
    )
    from co_scientist.orchestration.task_runtime import apply_task_update

    committed = cast(
        "dict[str, Any]",
        apply_task_update(cast("WorkflowState", state), _mature_reflection_update(state, items)),
    )
    checkpoint_seq, successor_id = await _checkpoint_and_advance(
        TaskCommit(task, current_seq, db_path),
        committed,
        "comprehensive_reflection",
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "successor_task_id": successor_id,
        "successful_reviews": items.successful,
        "failed_reviews": items.failed,
    }


def _mark_failed_item_unverified(by_id: dict[str, Any], item: Any) -> None:
    from co_scientist.science.reflection.deep_verification import (
        mark_hypothesis_unverified,
    )

    hypothesis = by_id.get(str(item.inputs.get("hypothesis_id") or ""))
    if hypothesis is not None:
        mark_hypothesis_unverified(hypothesis)


def _mark_verification_item(by_id: dict[str, Any], item: Any) -> None:
    """Failed verification still spends its issuance; otherwise recovery can
    repeat an unbounded failing wave.
    """
    from co_scientist.science.reflection.deep_verification import (
        mark_verification_issued,
    )

    hypothesis = by_id.get(str(item.inputs.get("hypothesis_id") or ""))
    if hypothesis is not None:
        mark_verification_issued(hypothesis)


def _completed_verification(item: Any) -> dict[str, Any] | None:
    if item.status != "completed" or not item.result:
        return None
    verification = item.result.get("verification")
    return verification if isinstance(verification, dict) else None


def _record_verification(hypothesis: Any, verification: dict[str, Any], model_name: str) -> None:
    from co_scientist.science.reflection.deep_verification import (
        verification_fingerprint,
    )

    hypothesis.deep_verification_probes = verification.get("probes", [])
    hypothesis.deep_verification_verdict = verification.get("verdict")
    hypothesis.deep_verification_fingerprint = verification_fingerprint(hypothesis, model_name)
    hypothesis.enrichments["deep_verification"] = verification


def _apply_verification_items(
    by_id: dict[str, Any],
    item_task_ids: Sequence[Any],
    db_path: str | None,
    model_name: str,
) -> _AppliedItems:
    from co_scientist.science.reflection import has_valid_verification

    successful = failed = llm_calls = 0
    verification_results: list[dict[str, Any]] = []
    usage_snapshots: list[dict[str, Any]] = []
    for item_id in item_task_ids:
        item = _require_item_task(item_id, db_path, kind="verification item")
        _mark_verification_item(by_id, item)
        verification = _completed_verification(item)
        if verification is not None:
            verification_results.append(verification)
        if not has_valid_verification(verification):
            failed += 1
            _mark_failed_item_unverified(by_id, item)
            continue
        assert verification is not None
        hypothesis = by_id[str((item.result or {})["hypothesis_id"])]
        _record_verification(hypothesis, verification, model_name)
        llm_calls += int(verification.get("verification_llm_calls", 1))
        usage_snapshots.append((item.result or {}).get("model_usage") or {})
        successful += 1
    return _AppliedItems(
        successful=successful,
        failed=failed,
        llm_calls=llm_calls,
        results=verification_results,
        model_usage=merge_usage_snapshots(usage_snapshots),
    )


def _verification_aggregate_update(
    state: dict[str, Any],
    items: _AppliedItems,
) -> dict[str, Any]:
    from co_scientist.domains.research_state.models import (
        MetricDeltas,
        create_metrics_update,
        phase_message,
    )
    from co_scientist.science.reflection.deep_verification import (
        merge_retrieved_articles,
    )

    state["articles"] = merge_retrieved_articles(
        state.get("articles"), cast("list[dict[str, Any] | None]", items.results)
    )
    return {
        "hypotheses": state["hypotheses"],
        "articles": state["articles"],
        "metrics": create_metrics_update(
            deltas=MetricDeltas(llm_calls=items.llm_calls),
            model_usage=items.model_usage,
        ),
        "messages": phase_message(
            "deep_verification",
            f"Deep-verified {items.successful} hypotheses; {items.failed} isolated failures",
        ),
    }


async def execute_verification_aggregate(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    replay, state, current_seq = leased_state(task, db_path, label="verification aggregate")
    if replay is not None:
        return replay
    by_id = {hypothesis.id: hypothesis for hypothesis in state["hypotheses"]}
    items = _apply_verification_items(
        by_id,
        task.inputs.get("item_task_ids", []),
        db_path,
        state["model_name"],
    )
    from co_scientist.orchestration.task_runtime import apply_task_update

    committed = cast(
        "dict[str, Any]",
        apply_task_update(
            cast("WorkflowState", state), _verification_aggregate_update(state, items)
        ),
    )
    checkpoint_seq, successor_id = await _checkpoint_and_advance(
        TaskCommit(task, current_seq, db_path), committed, "deep_verification"
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "successor_task_id": successor_id,
        "successful_verifications": items.successful,
        "failed_verifications": items.failed,
    }


@dataclass(frozen=True)
class _AggregateSpec:
    task_type: str
    priority: int
    key_prefix: str
    extra_inputs: Mapping[str, Any] = field(default_factory=dict)


def _enqueue_aggregate_task(
    task: ScientificTask,
    items: Sequence[ScientificTask],
    checkpoint_seq: int,
    conn: sqlite3.Connection,
    spec: _AggregateSpec,
) -> ScientificTask:
    return tasks.enqueue_task(
        NewTask(
            run_id=task.run_id,
            task_type=spec.task_type,
            inputs={
                "checkpoint_seq": checkpoint_seq,
                "item_task_ids": [item.id for item in items],
                **spec.extra_inputs,
            },
            idempotency_key=f"{spec.key_prefix}:aggregate:{checkpoint_seq}",
            priority=spec.priority,
            dependencies=tuple(item.id for item in items),
            provenance={
                "scheduled_by": task.task_type,
                "allow_failed_dependencies": True,
            },
        ),
        conn=conn,
    )


@dataclass(frozen=True)
class _AppliedReviews:
    successful: int
    failed: int
    llm_calls: int
    model_usage: dict[str, dict[str, Any]]


def _review_entries(item: Any) -> list[tuple[str, dict[str, Any] | None]]:
    if "reviews" in item.result:
        return [(str(entry["hypothesis_id"]), entry["review"]) for entry in item.result["reviews"]]
    return [(str(item.result["hypothesis_id"]), item.result["review"])]


def _item_hypothesis_ids(item: Any) -> list[str]:
    ids = item.inputs.get("hypothesis_ids")
    if ids is None:
        ids = [item.inputs.get("hypothesis_id", "")]
    return [str(hypothesis_id) for hypothesis_id in ids]


def _apply_review_items(
    by_id: dict[str, Any],
    item_task_ids: Sequence[Any],
    db_path: str | None,
    criteria: list[str] | None = None,
) -> _AppliedReviews:
    from co_scientist.domains.research_state.models import HypothesisReview
    from co_scientist.science.reflection import apply_initial_review_gate
    from co_scientist.science.reflection.review_gate import (
        refresh_review_dispositions,
    )

    successful = 0
    failed = 0
    usage_snapshots: list[dict[str, Any]] = []
    for item_id in item_task_ids:
        item = _require_item_task(item_id, db_path, kind="review item")
        completed = item.status == "completed" and bool(item.result)
        entries = (
            _review_entries(item)
            if completed
            else [(hypothesis_id, None) for hypothesis_id in _item_hypothesis_ids(item)]
        )
        for hypothesis_id, payload in entries:
            hypothesis = by_id.get(hypothesis_id)
            if payload is None:
                failed += 1
                if hypothesis is not None:
                    hypothesis.review_disposition = "review_failed"
                continue
            review = HypothesisReview(**payload)
            hypothesis = by_id[hypothesis_id]
            hypothesis.reviews.append(review)
            hypothesis.score = review.overall_score
            apply_initial_review_gate([hypothesis], [review], criteria)
            successful += 1
        if completed and item.result:
            usage_snapshots.append(item.result.get("model_usage") or {})
    refresh_review_dispositions(by_id.values(), criteria)
    return _AppliedReviews(
        successful=successful,
        failed=failed,
        llm_calls=len(item_task_ids),
        model_usage=merge_usage_snapshots(usage_snapshots),
    )


def _review_aggregate_update(state: dict[str, Any], applied: _AppliedReviews) -> dict[str, Any]:
    from co_scientist.domains.research_state.models import (
        MetricDeltas,
        create_metrics_update,
        phase_message,
    )

    return {
        "hypotheses": state["hypotheses"],
        "metrics": create_metrics_update(
            deltas=MetricDeltas(reviews=applied.successful, llm_calls=applied.llm_calls),
            model_usage=applied.model_usage,
        ),
        "messages": phase_message(
            "review",
            f"Reviewed {applied.successful} hypotheses; {applied.failed} isolated failures",
            strategy="durable_comparative_batches",
        ),
    }


async def execute_review_aggregate(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    replay, state, current_seq = leased_state(task, db_path, label="review aggregate")
    if replay is not None:
        return replay
    by_id = {hypothesis.id: hypothesis for hypothesis in state["hypotheses"]}
    applied = _apply_review_items(
        by_id,
        task.inputs.get("item_task_ids", []),
        db_path,
        criteria=state.get("criteria"),
    )
    from co_scientist.orchestration.task_runtime import apply_task_update

    committed = cast(
        "dict[str, Any]",
        apply_task_update(
            cast("WorkflowState", state),
            _review_aggregate_update(state, applied),
        ),
    )
    checkpoint_seq, successor_id = await _checkpoint_and_advance(
        TaskCommit(task, current_seq, db_path), committed, "review"
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "successor_task_id": successor_id,
        "successful_reviews": applied.successful,
        "failed_reviews": applied.failed,
    }


@dataclass(frozen=True)
class _GenerationItems:
    buckets: dict[str, list[Any]]
    transcripts: list[dict[str, Any]]
    failed: int
    llm_calls: int = 0
    model_usage: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills_used: dict[str, int] = field(default_factory=dict)


def _collect_generation_results(
    item_task_ids: Sequence[Any],
    db_path: str | None,
) -> _GenerationItems:
    from co_scientist.domains.research_state.models import Hypothesis

    buckets: dict[str, list[Any]] = {
        "tools": [],
        "debate_lit": [],
        "debate_only": [],
        "assumptions": [],
    }
    transcripts: list[dict[str, Any]] = []
    failed = 0
    llm_calls = 0
    usage_snapshots: list[dict[str, Any]] = []
    skills_used: dict[str, int] = {}
    for item_id in item_task_ids:
        item = _require_item_task(item_id, db_path, kind="generation strategy")
        if item.status != "completed" or not item.result:
            failed += 1
            continue
        strategy = str(item.result["strategy"])
        buckets[strategy].extend(
            Hypothesis.from_dict(payload) for payload in item.result.get("hypotheses", [])
        )
        transcripts.extend(item.result.get("transcripts", []))
        llm_calls += int(item.result.get("llm_calls", 0))
        usage_snapshots.append(item.result.get("model_usage") or {})
        for name, uses in (item.result.get("skills_used") or {}).items():
            skills_used[name] = skills_used.get(name, 0) + int(uses)
    return _GenerationItems(
        buckets,
        transcripts,
        failed,
        llm_calls,
        merge_usage_snapshots(usage_snapshots),
        skills_used,
    )


async def _generation_aggregate_update(
    task: ScientificTask,
    state: dict[str, Any],
    items: _GenerationItems,
) -> dict[str, Any]:
    from co_scientist.domains.research_state.models import MetricDeltas, create_metrics_update
    from co_scientist.science.generation import (
        GenerationCounts,
        GenerationResults,
        finalize_generation,
    )

    buckets = items.buckets
    update: dict[str, Any] = await finalize_generation(
        cast("WorkflowState", state),
        GenerationCounts(**task.inputs["counts"]),
        GenerationResults(
            tools_hypotheses=buckets["tools"],
            debate_with_lit_hypotheses=buckets["debate_lit"],
            debate_only_hypotheses=buckets["debate_only"],
            assumptions_hypotheses=buckets["assumptions"],
            debate_transcripts=items.transcripts,
            llm_call_count=items.llm_calls,
        ),
    )
    update["metrics"] = create_metrics_update(
        hypothesis_count=update["hypothesis_count"],
        deltas=MetricDeltas(
            llm_calls=update.get("llm_call_count", 0),
            skills_used=items.skills_used,
        ),
        model_usage=items.model_usage,
    )
    if items.failed:
        update["message"] += f"; {items.failed} strategy failure(s) isolated"
    return update


async def execute_generation_aggregate(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    replay, state, current_seq = leased_state(task, db_path, label="generation aggregate")
    if replay is not None:
        return replay
    items = _collect_generation_results(task.inputs.get("item_task_ids", []), db_path)
    from co_scientist.orchestration.task_runtime import apply_task_update

    update = await _generation_aggregate_update(task, state, items)
    committed = cast(
        "dict[str, Any]",
        apply_task_update(cast("WorkflowState", state), update),
    )
    checkpoint_seq, successor_id = await _checkpoint_and_advance(
        TaskCommit(task, current_seq, db_path), committed, "generate"
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "successor_task_id": successor_id,
        "hypotheses_generated": update["hypothesis_count"],
        "failed_strategies": items.failed,
    }
