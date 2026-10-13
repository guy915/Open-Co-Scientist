from __future__ import annotations

import asyncio
import random
from typing import Any, cast

import pytest

from co_scientist.domains.research_state.models import ExecutionMetrics
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.orchestration.checkpoint import (
    restore_workflow_state,
    serialize_workflow_state,
)
from co_scientist.platform.llm.offline.llm import (
    _GENERATED_VOCABULARY,
    leaf_text,
    subject_terms,
)
from co_scientist.platform.llm.structured.validate import get_fallback_response
from co_scientist.platform.telemetry.progress import _ACTIVE_WORKFLOW_STATE, emit_progress
from tests._mcp import isolate_offline_router


def test_generated_text_is_never_mined_as_subject_matter() -> None:
    """Mining generated parent text compounds filler across evolution cycles."""
    rng = random.Random(0)
    generated = leaf_text(rng, 1, "statement", ("resistance", "biofilms"))

    terms = subject_terms(f"Original Hypothesis: {generated}")

    assert not set(terms) & _GENERATED_VOCABULARY


def _fresh_state(**extra: Any) -> WorkflowState:
    state: dict[str, Any] = {"degraded_nodes": []}
    state.update(extra)
    return cast(WorkflowState, state)


async def test_degradation_event_failure_cannot_break_the_run() -> None:

    async def callback(event: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("listener exploded")

    state = _fresh_state()
    await emit_progress(state, "phase", "working", 10)
    # Installed after the stash so the raising listener only hears the
    # degradation event under test.
    state["progress_callback"] = callback

    fallback = get_fallback_response({"name": "meta_review"})
    await asyncio.sleep(0)

    assert fallback is not None
    assert state["degraded_nodes"] == ["meta_review"]


def test_durable_commit_captures_recorded_degradation() -> None:
    """Mid-node fallback appends must survive whole-state checkpoint commits."""
    from co_scientist.orchestration.task_runtime import apply_task_update

    state = _fresh_state()
    _ACTIVE_WORKFLOW_STATE.set(state)

    get_fallback_response({"name": "meta_review"})
    committed = apply_task_update(state, {"meta_review": {}})

    assert committed["degraded_nodes"] == ["meta_review"]


def test_checkpoint_round_trips_degraded_nodes() -> None:
    state: dict[str, Any] = {
        "hypotheses": [],
        "articles": None,
        "messages": [],
        "metrics": ExecutionMetrics(),
        "degraded_nodes": ["meta_review", "research_overview"],
    }

    envelope = serialize_workflow_state(state, last_event_seq=3)
    assert envelope["state"]["degraded_nodes"] == [
        "meta_review",
        "research_overview",
    ]

    restored = restore_workflow_state(envelope)
    assert restored["degraded_nodes"] == [
        "meta_review",
        "research_overview",
    ]


@pytest.fixture(autouse=True)
def _offline_isolate_offline_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installed router is process-wide and must be restored between
    tests."""
    isolate_offline_router(monkeypatch)
