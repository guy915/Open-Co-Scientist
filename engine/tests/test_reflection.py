from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import co_scientist.science.reflection.deep_verification as leaf
from co_scientist.core.exceptions import (
    LLMCallBudgetExceededError,
    LLMRateLimitParkError,
)
from co_scientist.orchestration.generator import run_setup
from co_scientist.platform.retrieval.article import Article
from co_scientist.platform.sandbox.workspace.session import WorkspaceSession
from co_scientist.science import evidence_context
from co_scientist.science.reflection import (
    ReviewRun,
    ReviewType,
    apply_initial_review_gate,
    observe_hypothesis,
    review_hypothesis,
    select_hypotheses_to_verify,
    verify_hypothesis,
)
from co_scientist.science.reflection import comprehensive_reflection as cr
from co_scientist.science.reflection import (
    comprehensive_reflection as review_prompt_context,
)
from co_scientist.science.reflection import deep_verification as dv
from co_scientist.science.reflection import reflection as observation
from co_scientist.science.reflection import review_evidence as ev
from co_scientist.science.reflection import simulation_execution as se
from co_scientist.science.reflection.deep_verification import (
    mark_verification_issued,
    verification_fingerprint,
)
from co_scientist.science.reflection.reflection import reflection_node
from co_scientist.science.reflection.review_evidence import (
    ReviewResearch,
)
from tests._llm_fake import mock_call_llm_json
from tests._state import make_article, make_hypothesis, make_review, make_state

_ARTICLES = "Article 1: observation A supports pathway X."


async def test_observation_is_left_to_the_finalist_review() -> None:
    pool = [make_hypothesis(text="a", reviews=[make_review()], win_count=1)]
    state = make_state(hypotheses=pool, articles_with_reasoning=_ARTICLES)

    assert await reflection_node(state) == {}
    assert pool[0].reflection_notes is None


def _stub_review_research(monkeypatch: pytest.MonkeyPatch, *, fails: bool = False) -> None:

    async def fake_research(_state: object, _hypothesis: object) -> object:
        if fails:
            raise RuntimeError("source unreachable")
        return ReviewResearch(
            articles=[
                Article(
                    title="Researched paper",
                    source_id="researched-1",
                    abstract="Human evidence for the mechanism.",
                    retrieval_call_id="call-1",
                )
            ],
            ledger={"goal": "g", "threads": [], "calls": [], "findings": []},
        )

    monkeypatch.setattr(ev, "research_for_review", fake_research)


async def test_the_node_carries_every_hypothesis_ledger_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ledgers kept inside reviews never reach the persistence drain."""
    mock_call_llm_json(monkeypatch, cr, {"verdict": "sound"})
    _stub_review_research(monkeypatch)
    viable = [
        make_hypothesis(text=text, reviews=[make_review()], win_count=1) for text in ("a", "b")
    ]
    for hypothesis in viable:
        hypothesis.review_disposition = "viable"

    result = await cr.comprehensive_reflection_node(
        make_state(hypotheses=viable, current_iteration=0)
    )

    assert len(result["research_ledgers"]) == 2


def test_retracted_evidence_never_reaches_a_prompt() -> None:
    """Retraction checks belong at every formatting boundary, not only
    retrieval."""
    retracted = make_article(
        "Retracted paper",
        abstract="Withdrawn mechanistic claim.",
        used_in_analysis=True,
        is_retracted=True,
    )
    only_retracted = make_state(articles=[retracted])
    assert review_prompt_context._build_domain_context(only_retracted, None) == ""
    assert evidence_context._retrieved_evidence_context([retracted]) == ""
    state = make_state(
        articles=[
            make_article(
                "Retracted paper",
                abstract="Withdrawn mechanistic claim.",
                used_in_analysis=True,
                is_retracted=True,
            ),
            make_article(
                "Standing paper",
                abstract="Replicated mechanistic finding.",
                used_in_analysis=True,
            ),
        ]
    )

    context = dv._verification_evidence_context(state)

    assert "Standing paper" in context
    assert "Retracted paper" not in context
    assert "Withdrawn mechanistic claim" not in context


def test_gate_honors_scientist_criteria_and_safety() -> None:
    ideas = [make_hypothesis(), make_hypothesis()]
    reviews = [
        make_review(scores={"novelty": 1, "testability": 8, "safety": 8}),
        make_review(scores={"testability": 8, "safety": 1}),
    ]
    apply_initial_review_gate(ideas, reviews, ["Experimental feasibility"])
    assert [idea.review_disposition for idea in ideas] == ["viable", "unsafe"]


def test_selection_preserves_pool_order_and_once_ever_markers() -> None:
    pending = make_hypothesis()
    blocked = make_hypothesis(review_disposition="unsafe")
    issued = make_hypothesis()
    mark_verification_issued(issued)
    current = make_hypothesis()
    current.deep_verification_fingerprint = verification_fingerprint(current, "m")
    next_pending = make_hypothesis()
    assert select_hypotheses_to_verify([pending, blocked, issued, current, next_pending], "m") == [
        pending,
        next_pending,
    ]
    assert not pending.enrichments


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,owner",
    [
        (verify_hypothesis, leaf),
        (observe_hypothesis, observation),
        (review_hypothesis, cr),
    ],
)
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("ordinary"),
        LLMRateLimitParkError(9999, "cap"),
        LLMCallBudgetExceededError(2, 1),
    ],
)
async def test_failures_degrade_but_task_control_propagates(
    monkeypatch: pytest.MonkeyPatch,
    operation: Any,
    owner: Any,
    error: Exception,
) -> None:
    mock_call_llm_json(monkeypatch, owner, side_effect=error)
    state = make_state(articles_with_reasoning="Retrieved literature")
    args = (
        (state, make_hypothesis(), ReviewType.FULL)
        if operation is review_hypothesis
        else (state, make_hypothesis())
    )
    if isinstance(error, RuntimeError):
        result = await operation(*args)
        assert (result.result if isinstance(result, ReviewRun) else result) is None
    else:
        with pytest.raises(type(error)) as caught:
            await operation(*args)
        assert caught.value is error


# Offer run_command explicitly so these tests exercise the loop on every host.
_RUNNABLE_TOOLS = [{"function": {"name": "run_command"}}]


def _reflection_simulation_execution_state(**overrides: Any) -> Any:
    state = make_state(hypotheses=[], current_iteration=0)
    for key, value in overrides.items():
        state[key] = value  # type: ignore[literal-required]
    return state


class TestTheOfflineBackendNeverExecutes:
    def test_an_offline_run_stays_mental(self) -> None:
        # Offline replies emit no tool calls, so an execution loop would observe
        # nothing.
        assert (
            run_setup._resolve_simulation_execution(
                {"enable_simulation_execution": True}, "offline/deterministic"
            )
            is False
        )


class TestSimulationConfinementAdmission:
    def test_requested_simulation_is_disabled_with_the_exact_capability_reason(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        reason = "no delegated cgroup v2 lifecycle boundary"
        monkeypatch.setattr(run_setup, "command_execution_unavailable_reason", lambda: reason)

        assert (
            run_setup._resolve_simulation_execution(
                {"enable_simulation_execution": True}, "openai/model"
            )
            is False
        )
        assert reason in caplog.text

    def test_requested_simulation_remains_enabled_when_confinement_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(run_setup, "command_execution_unavailable_reason", lambda: None)

        assert (
            run_setup._resolve_simulation_execution(
                {"enable_simulation_execution": True}, "openai/model"
            )
            is True
        )

    def test_simulation_review_provenance_records_no_execution(self) -> None:
        result: dict[str, Any] = {}
        evidence = ev._ReviewEvidence(queries=[], articles=[], errors=[])

        cr._record_review_provenance(result, evidence, [], ReviewType.SIMULATION, observations=None)

        assert result["executed"] is False
        assert "execution_observations" not in result


class TestDegradation:
    async def test_a_failing_loop_reviews_mentally(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(se, "workspace_tool_schemas", lambda policy: _RUNNABLE_TOOLS)
        monkeypatch.setattr(
            se,
            "open_review_workspace",
            lambda *a, **k: WorkspaceSession(tmp_path),
        )
        monkeypatch.setattr(
            se,
            "call_llm_with_tools",
            AsyncMock(side_effect=RuntimeError("provider fell over")),
        )

        assert (
            await se.simulation_observations(
                _reflection_simulation_execution_state(run_id="r1"),
                make_hypothesis(text="a"),
            )
            is None
        )


class TestIsolation:
    def test_two_reviews_of_one_run_do_not_share_a_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Concurrent leased reviews sharing a directory would report
        observations of each other's models."""
        from co_scientist.platform.sandbox.workspace import run_workspace

        monkeypatch.setattr(run_workspace, "workspaces_root", lambda: tmp_path)

        first = run_workspace.open_review_workspace("run-1", "hyp-a")
        second = run_workspace.open_review_workspace("run-1", "hyp-b")

        assert first.root != second.root
        assert first.root.is_dir() and second.root.is_dir()
