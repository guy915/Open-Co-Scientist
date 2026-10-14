from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from co_scientist.core.constants import MAX_PLANNING_IDEAS
from co_scientist.orchestration.engine_tasks.fanout_aggregates import (
    _AggregateSpec,
    _enqueue_aggregate_task,
)
from co_scientist.orchestration.engine_tasks.support import (
    GENERATION_AGGREGATE_TASK,
    GENERATION_STRATEGY_TASK,
    MATURE_REFLECTION_AGGREGATE_TASK,
    MATURE_REFLECTION_ITEM_TASK,
    REVIEW_AGGREGATE_TASK,
    REVIEW_ITEM_TASK,
    VERIFICATION_AGGREGATE_TASK,
    VERIFICATION_ITEM_TASK,
    _restore_item_checkpoint,
    _save_exact_checkpoint,
    assert_task_commit_allowed,
)
from co_scientist.orchestration.repository import events, tasks
from co_scientist.orchestration.repository.tasks import NewTask
from co_scientist.platform import db
from co_scientist.platform.db.models import ScientificTask

if TYPE_CHECKING:
    from co_scientist.domains.research_state.state import WorkflowState


def _hypothesis_for_item(task: ScientificTask, state: dict[str, Any]) -> tuple[str, Any]:
    hypothesis_id = str(task.inputs["hypothesis_id"])
    hypothesis = next(
        (item for item in state["hypotheses"] if item.id == hypothesis_id),
        None,
    )
    if hypothesis is None:
        raise ValueError(f"hypothesis {hypothesis_id} is absent from checkpoint")
    return hypothesis_id, hypothesis


async def execute_review_item(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    from co_scientist.domains.research_state.models import HypothesisReview
    from co_scientist.platform.llm import scoped_telemetry
    from co_scientist.science.reflection.review import (
        ReviewContext,
        review_comparative_batch,
        review_single_hypothesis,
    )

    state, expected_seq = _restore_item_checkpoint(task, db_path, superseded="review item")
    context = ReviewContext.from_state(cast("WorkflowState", state))
    if "hypothesis_ids" not in task.inputs:
        # Items enqueued before batching review one idea each.
        hypothesis_id, hypothesis = _hypothesis_for_item(task, state)
        with scoped_telemetry("review") as telemetry:
            review = await review_single_hypothesis(
                hypothesis_text=hypothesis.text,
                context=context,
                hypothesis_index=int(task.inputs["hypothesis_index"]),
            )
        return {
            "hypothesis_id": hypothesis_id,
            "review": dataclasses.asdict(review),
            "model_usage": telemetry.snapshot(),
            "checkpoint_seq": expected_seq,
        }
    by_id = {item.id: item for item in state["hypotheses"]}
    ids = [str(hypothesis_id) for hypothesis_id in task.inputs["hypothesis_ids"]]
    missing = [hypothesis_id for hypothesis_id in ids if hypothesis_id not in by_id]
    if missing:
        raise ValueError(f"hypotheses {missing} are absent from checkpoint")
    hypotheses = [by_id[hypothesis_id] for hypothesis_id in ids]
    with scoped_telemetry("review") as telemetry:
        reviews: list[HypothesisReview | None]
        if len(hypotheses) == 1:
            reviews = [
                await review_single_hypothesis(
                    hypothesis_text=hypotheses[0].text, context=context, hypothesis_index=0
                )
            ]
        else:
            reviews = await review_comparative_batch(hypotheses, context)
    return {
        "reviews": [
            {
                "hypothesis_id": hypothesis_id,
                "review": dataclasses.asdict(review) if review is not None else None,
            }
            for hypothesis_id, review in zip(ids, reviews, strict=True)
        ],
        "model_usage": telemetry.snapshot(),
        "checkpoint_seq": expected_seq,
    }


async def execute_verification_item(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    from co_scientist.platform.llm import scoped_telemetry
    from co_scientist.science.reflection import verify_hypothesis

    state, expected_seq = _restore_item_checkpoint(task, db_path, superseded="verification item")
    hypothesis_id, hypothesis = _hypothesis_for_item(task, state)
    with scoped_telemetry("deep_verification") as telemetry:
        result = await verify_hypothesis(cast("WorkflowState", state), hypothesis)
    if result is None:
        raise RuntimeError(f"deep verification failed for {hypothesis_id}")
    return {
        "hypothesis_id": hypothesis_id,
        "verification": result,
        "model_usage": telemetry.snapshot(),
        "checkpoint_seq": expected_seq,
    }


async def execute_mature_reflection_item(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    from co_scientist.platform.llm import scoped_telemetry
    from co_scientist.science.reflection import ReviewType, review_hypothesis

    state, expected_seq = _restore_item_checkpoint(task, db_path, superseded="mature reflection")
    hypothesis_id, hypothesis = _hypothesis_for_item(task, state)
    mode = ReviewType(str(task.inputs["review_mode"]))
    with scoped_telemetry("comprehensive_reflection") as telemetry:
        ledger: dict[str, Any] | None = None
        if mode is ReviewType.FINALIST:
            from co_scientist.science.reflection.comprehensive_reflection import review_finalist

            _, result, ledger = await review_finalist(cast("WorkflowState", state), hypothesis)
        elif mode is ReviewType.OBSERVATION:
            from co_scientist.science.reflection import observe_hypothesis

            if not state.get("articles_with_reasoning"):
                raise RuntimeError("observation review has no literature context")
            result = await observe_hypothesis(cast("WorkflowState", state), hypothesis)
        else:
            _, result, ledger = await review_hypothesis(
                cast("WorkflowState", state), hypothesis, mode
            )
    if result is None:
        raise RuntimeError(f"{mode.value} review failed for {hypothesis_id}")
    return {
        "hypothesis_id": hypothesis_id,
        "review_mode": mode.value,
        "review": result,
        # Retrieval ledgers attach to run state, not review enrichments that
        # would recarry them through every checkpoint.
        "research_ledger": ledger,
        "model_usage": telemetry.snapshot(),
        "checkpoint_seq": expected_seq,
    }


@dataclass(frozen=True)
class _StrategyInputs:
    literature: Any
    reference_index: Any


@dataclass(frozen=True)
class _GenerationPlan:
    task_specs: list[tuple[str, int, int, int]]
    inputs: _StrategyInputs
    aggregate_spec: _AggregateSpec


def _generation_task_specs(
    strategy_counts: dict[str, int],
) -> list[tuple[str, int, int, int]]:
    """Each debate carries its index and whole batch size for diversity;
    splitting batched drafting would multiply calls and writer
    contention.
    """
    debate_strategies = {"debate_lit", "debate_only"}
    if (
        any(type(count) is not int or count < 0 for count in strategy_counts.values())
        or sum(strategy_counts.values()) > MAX_PLANNING_IDEAS
    ):
        raise ValueError("generation fan-out exceeds the admitted idea budget")
    return [
        (strategy, 1, index, count)
        for strategy, count in strategy_counts.items()
        if strategy in debate_strategies
        for index in range(count)
    ] + [
        (strategy, count, 0, count)
        for strategy, count in strategy_counts.items()
        if strategy not in debate_strategies and count > 0
    ]


def _enqueue_generation_strategy_tasks(
    task: ScientificTask,
    planned_seq: int,
    task_specs: Sequence[tuple[str, int, int, int]],
    inputs: _StrategyInputs,
    conn: sqlite3.Connection,
) -> list[ScientificTask]:
    if (
        len(task_specs) > MAX_PLANNING_IDEAS
        or sum(count for _, count, _, _ in task_specs) > MAX_PLANNING_IDEAS
    ):
        raise ValueError("generation fan-out exceeds the admitted idea budget")
    return [
        tasks.enqueue_task(
            NewTask(
                run_id=task.run_id,
                task_type=GENERATION_STRATEGY_TASK,
                inputs={
                    "checkpoint_seq": planned_seq,
                    "strategy": strategy,
                    "count": count,
                    "strategy_index": index,
                    # Whole batch size gives each debate its sibling-relative
                    # diversity angle; legacy inputs omit the angle.
                    "debate_total": total,
                    "literature": inputs.literature,
                    "reference_text": inputs.reference_index.text,
                    "reference_sources": inputs.reference_index.sources,
                },
                idempotency_key=f"generation:{strategy}:{planned_seq}:{index}:{count}",
                priority=87,
                dependencies=(task.id,),
                provenance={
                    "scheduled_by": task.task_type,
                    "generation_strategy": strategy,
                    "strategy_index": index,
                },
            ),
            conn=conn,
        )
        for strategy, count, index, total in task_specs
    ]


async def _plan_generation_fanout(state: dict[str, Any]) -> _GenerationPlan:
    """Provider preparation completes before any plan transaction acquires
    SQLite's writer.
    """
    from co_scientist.science.generation import prepare_generation

    plan = await prepare_generation(cast("WorkflowState", state))
    return _GenerationPlan(
        task_specs=_generation_task_specs(plan.counts.strategy_counts),
        inputs=_StrategyInputs(plan.literature, plan.reference_index),
        aggregate_spec=_AggregateSpec(
            task_type=GENERATION_AGGREGATE_TASK,
            priority=81,
            key_prefix="generation",
            extra_inputs={"counts": dataclasses.asdict(plan.counts)},
        ),
    )


def _commit_generation_fanout(
    task: ScientificTask,
    checkpoint_seq: int,
    envelope: dict[str, Any],
    plan: _GenerationPlan,
    db_path: str | None,
) -> tuple[int, list[ScientificTask], ScientificTask]:
    """Planning checkpoint and all fan-out rows commit together, leaving no
    partially scheduled wave.
    """
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        planned_seq = _save_exact_checkpoint(
            task,
            envelope,
            checkpoint_seq,
            conn,
            changed_message="checkpoint changed during generation planning",
        )
        items = _enqueue_generation_strategy_tasks(
            task,
            planned_seq,
            plan.task_specs,
            plan.inputs,
            conn,
        )
        aggregate = _enqueue_aggregate_task(
            task,
            items,
            planned_seq,
            conn,
            plan.aggregate_spec,
        )
    return planned_seq, items, aggregate


async def _enqueue_generation_fanout(
    task: ScientificTask,
    state: dict[str, Any],
    checkpoint_seq: int,
    *,
    db_path: str | None,
) -> dict[str, Any]:
    from co_scientist.orchestration.checkpoint import serialize_workflow_state

    plan = await _plan_generation_fanout(state)
    envelope = serialize_workflow_state(
        state,
        last_event_seq=events.latest_event_seq(task.run_id, db_path=db_path),
    )
    planned_seq, items, aggregate = _commit_generation_fanout(
        task, checkpoint_seq, envelope, plan, db_path
    )
    return {
        "checkpoint_seq": planned_seq,
        "fanout_task_ids": [item.id for item in items],
        "aggregate_task_id": aggregate.id,
        "node": "generate",
    }


@dataclass(frozen=True)
class _StrategyRunInputs:
    reference_index: Any
    literature: Any
    debate_index: int | None = None
    debate_total: int | None = None


async def _run_debate_strategy(
    state: dict[str, Any],
    strategy: str,
    count: int,
    inputs: _StrategyRunInputs,
) -> tuple[list[Any], list[dict[str, Any]], int]:
    """Parallel debates need their sibling index and whole batch size to
    select distinct diversity angles.
    """
    from co_scientist.science.citations import ReferenceIndex
    from co_scientist.science.generation.debate import (
        DebateBatchPosition,
        generate_with_debate,
    )

    literature = inputs.literature if strategy == "debate_lit" else None
    debate_reference = (
        inputs.reference_index if strategy == "debate_lit" else ReferenceIndex(text="", sources={})
    )
    batch_position = None
    if inputs.debate_index is not None and inputs.debate_total is not None:
        batch_position = DebateBatchPosition(inputs.debate_index, inputs.debate_total)
    result: tuple[list[Any], list[dict[str, Any]], int] = await generate_with_debate(
        state=cast("WorkflowState", state),
        count=count,
        articles_with_reasoning=literature,
        reference_index=debate_reference,
        batch_position=batch_position,
    )
    return result


async def _run_generation_strategy(
    state: dict[str, Any],
    strategy: str,
    count: int,
    inputs: _StrategyRunInputs,
) -> tuple[list[Any], list[dict[str, Any]], int]:
    from co_scientist.science.generation.assumptions import (
        generate_with_assumptions,
    )
    from co_scientist.science.generation.literature_tools import (
        generate_with_tools,
    )

    if strategy == "tools":
        hypotheses, llm_calls = await generate_with_tools(
            cast("WorkflowState", state), count, inputs.reference_index
        )
        return hypotheses, [], llm_calls
    if strategy in {"debate_lit", "debate_only"}:
        return await _run_debate_strategy(state, strategy, count, inputs)
    if strategy == "assumptions":
        # Durable assumptions generation omits plan literature and references;
        # sharing coordinator dispatch would alter grounding.
        hypotheses, llm_calls = await generate_with_assumptions(cast("WorkflowState", state), count)
        return hypotheses, [], llm_calls
    raise ValueError(f"unsupported generation strategy: {strategy}")


async def execute_generation_strategy(
    task: ScientificTask, *, db_path: str | None = None
) -> dict[str, Any]:
    """Legacy debate inputs lack sibling angles; actual science-skill source
    usage travels to the report for attribution.
    """
    from co_scientist.platform.llm import scoped_telemetry
    from co_scientist.platform.sandbox.skills import scoped_skill_usage
    from co_scientist.science.citations import ReferenceIndex

    state, expected_seq = _restore_item_checkpoint(task, db_path, superseded="generation strategy")
    strategy = str(task.inputs["strategy"])
    count = int(task.inputs["count"])
    inputs = _StrategyRunInputs(
        reference_index=ReferenceIndex(
            text=str(task.inputs.get("reference_text") or ""),
            sources=dict(task.inputs.get("reference_sources") or {}),
        ),
        literature=task.inputs.get("literature"),
        debate_index=int(task.inputs.get("strategy_index") or 0),
        debate_total=int(task.inputs.get("debate_total") or count),
    )
    with (
        scoped_telemetry("generate") as telemetry,
        scoped_skill_usage() as skills,
    ):
        hypotheses, transcripts, llm_calls = await _run_generation_strategy(
            state, strategy, count, inputs
        )
    return {
        "strategy": strategy,
        "hypotheses": [hypothesis.to_dict() for hypothesis in hypotheses],
        "transcripts": transcripts,
        "llm_calls": llm_calls,
        "model_usage": telemetry.snapshot(),
        "skills_used": skills.snapshot(),
        "checkpoint_seq": expected_seq,
    }


# One comparative call screens a few ideas; a larger batch forces a wider
# score spread on peers that may all be viable.
SCREENING_BATCH_SIZE = 4


def _enqueue_review_item_tasks(
    task: ScientificTask,
    unreviewed: Sequence[Any],
    checkpoint_seq: int,
    conn: sqlite3.Connection,
) -> list[ScientificTask]:
    chunks = [
        list(unreviewed[start : start + SCREENING_BATCH_SIZE])
        for start in range(0, len(unreviewed), SCREENING_BATCH_SIZE)
    ]
    return [
        tasks.enqueue_task(
            NewTask(
                run_id=task.run_id,
                task_type=REVIEW_ITEM_TASK,
                inputs={
                    "checkpoint_seq": checkpoint_seq,
                    "hypothesis_ids": [hypothesis.id for hypothesis in chunk],
                },
                idempotency_key=f"review:batch:{checkpoint_seq}:{chunk[0].id}",
                priority=85,
                dependencies=(task.id,),
                provenance={
                    "scheduled_by": task.task_type,
                    "specialist": "reflection.initial",
                },
            ),
            conn=conn,
        )
        for chunk in chunks
    ]


_REVIEW_AGGREGATE_SPEC = _AggregateSpec(
    task_type=REVIEW_AGGREGATE_TASK, priority=80, key_prefix="review"
)


def _create_fanout_tasks(
    enqueue_items: Callable[[sqlite3.Connection], list[ScientificTask]],
    task: ScientificTask,
    checkpoint_seq: int,
    spec: _AggregateSpec,
    db_path: str | None,
) -> tuple[list[ScientificTask], ScientificTask]:
    """Item tasks and their aggregate commit together so no partial family
    can be observed.
    """
    with db.transaction(db_path) as conn:
        assert_task_commit_allowed(task, conn)
        items = enqueue_items(conn)
        aggregate = _enqueue_aggregate_task(task, items, checkpoint_seq, conn, spec)
    return items, aggregate


def _enqueue_review_fanout(
    task: ScientificTask,
    state: dict[str, Any],
    checkpoint_seq: int,
    *,
    db_path: str | None,
) -> dict[str, Any]:
    """Durable review leases one comparative batch per few ideas, so a
    failed batch costs only its own ideas a retry next cycle."""
    from co_scientist.domains.research_state.models import has_peer_review

    unreviewed = [
        hypothesis for hypothesis in state["hypotheses"] if not has_peer_review(hypothesis)
    ]
    items, aggregate = _create_fanout_tasks(
        partial(_enqueue_review_item_tasks, task, unreviewed, checkpoint_seq),
        task,
        checkpoint_seq,
        _REVIEW_AGGREGATE_SPEC,
        db_path,
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "fanout_task_ids": [item.id for item in items],
        "aggregate_task_id": aggregate.id,
        "node": "review",
    }


def _enqueue_verification_item_tasks(
    task: ScientificTask,
    selected: Sequence[Any],
    checkpoint_seq: int,
    conn: sqlite3.Connection,
) -> list[ScientificTask]:
    return [
        tasks.enqueue_task(
            NewTask(
                run_id=task.run_id,
                task_type=VERIFICATION_ITEM_TASK,
                inputs={
                    "checkpoint_seq": checkpoint_seq,
                    "hypothesis_id": hypothesis.id,
                },
                idempotency_key=f"verification:item:{checkpoint_seq}:{hypothesis.id}",
                priority=88,
                dependencies=(task.id,),
                provenance={
                    "scheduled_by": task.task_type,
                    "specialist": "reflection.deep_verification",
                },
            ),
            conn=conn,
        )
        for hypothesis in selected
    ]


_VERIFICATION_AGGREGATE_SPEC = _AggregateSpec(
    task_type=VERIFICATION_AGGREGATE_TASK,
    priority=82,
    key_prefix="verification",
)


def _enqueue_verification_fanout(
    task: ScientificTask,
    state: dict[str, Any],
    checkpoint_seq: int,
    *,
    db_path: str | None,
) -> dict[str, Any]:
    from co_scientist.science.reflection import select_hypotheses_to_verify
    from co_scientist.science.scheduling.funnel import finalists

    selected = select_hypotheses_to_verify(
        finalists(cast("WorkflowState", state)), state["model_name"]
    )
    items, aggregate = _create_fanout_tasks(
        partial(_enqueue_verification_item_tasks, task, selected, checkpoint_seq),
        task,
        checkpoint_seq,
        _VERIFICATION_AGGREGATE_SPEC,
        db_path,
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "fanout_task_ids": [item.id for item in items],
        "aggregate_task_id": aggregate.id,
        "node": "deep_verification",
    }


class _ReflectionSpec(NamedTuple):
    hypothesis_id: str
    review_mode: str
    recheck: bool = False


def _recheck_specs(state: dict[str, Any]) -> list[_ReflectionSpec]:
    """Initially blocked ideas still receive their bounded recheck;
    checkpointed issuance survives recovery.
    """
    from co_scientist.science.reflection.review_gate import (
        RECHECK_REVIEW_TYPE,
        recheck_targets,
    )

    return [
        _ReflectionSpec(hypothesis.id, RECHECK_REVIEW_TYPE.value, True)
        for hypothesis in recheck_targets(state["hypotheses"])
    ]


def _mature_reflection_specs(state: dict[str, Any]) -> list[_ReflectionSpec]:
    """Each viable finalist gets one in-depth review; blocked ideas keep
    their one recheck except in the terminal pass, which nothing ranks
    afterwards."""
    from co_scientist.science.reflection.review_gate import ReviewType, finalist_review_needed
    from co_scientist.science.scheduling.funnel import (
        finalists,
        has_depth,
        is_terminal_depth_pass,
    )

    workflow = cast("WorkflowState", state)
    terminal = is_terminal_depth_pass(workflow)
    specs = [
        _ReflectionSpec(hypothesis.id, ReviewType.FINALIST.value)
        for hypothesis in finalists(workflow)
        if hypothesis.review_disposition == "viable"
        and finalist_review_needed(hypothesis)
        and not (terminal and has_depth(hypothesis))
    ]
    return specs if terminal else specs + _recheck_specs(state)


def _enqueue_mature_reflection_item_tasks(
    task: ScientificTask,
    specs: Sequence[_ReflectionSpec],
    checkpoint_seq: int,
    conn: sqlite3.Connection,
) -> list[ScientificTask]:
    return [
        tasks.enqueue_task(
            NewTask(
                run_id=task.run_id,
                task_type=MATURE_REFLECTION_ITEM_TASK,
                inputs={
                    "checkpoint_seq": checkpoint_seq,
                    "hypothesis_id": spec.hypothesis_id,
                    "review_mode": spec.review_mode,
                    "recheck": spec.recheck,
                },
                idempotency_key=f"reflection:{spec.review_mode}:{checkpoint_seq}:{spec.hypothesis_id}",
                priority=86,
                dependencies=(task.id,),
                provenance={
                    "scheduled_by": task.task_type,
                    "reflection_mode": spec.review_mode,
                },
            ),
            conn=conn,
        )
        for spec in specs
    ]


_MATURE_REFLECTION_AGGREGATE_SPEC = _AggregateSpec(
    task_type=MATURE_REFLECTION_AGGREGATE_TASK,
    priority=80,
    key_prefix="reflection",
)


def _enqueue_mature_reflection_fanout(
    task: ScientificTask,
    state: dict[str, Any],
    checkpoint_seq: int,
    *,
    db_path: str | None,
) -> dict[str, Any]:
    specs = _mature_reflection_specs(state)
    items, aggregate = _create_fanout_tasks(
        partial(_enqueue_mature_reflection_item_tasks, task, specs, checkpoint_seq),
        task,
        checkpoint_seq,
        _MATURE_REFLECTION_AGGREGATE_SPEC,
        db_path,
    )
    return {
        "checkpoint_seq": checkpoint_seq,
        "fanout_task_ids": [item.id for item in items],
        "aggregate_task_id": aggregate.id,
        "node": "comprehensive_reflection",
    }
