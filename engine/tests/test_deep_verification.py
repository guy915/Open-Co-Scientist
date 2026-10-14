from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest

import co_scientist.science.reflection.deep_verification as leaf
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science import evidence_context as dve
from co_scientist.science.reflection import deep_verification as dv
from tests._llm_fake import mock_call_llm_json
from tests._state import (
    make_article,
    make_hypothesis,
    make_review,
    make_state,
    make_verification_response,
)


def _finalist(text: str, **overrides: object) -> Hypothesis:
    """A reviewed idea alone in its pool is a finalist: depth goes only there."""
    return make_hypothesis(text=text, reviews=[make_review()], **overrides)


def _state() -> WorkflowState:
    return cast(WorkflowState, {"run_id": "run-1"})


async def test_a_resumed_run_does_not_re_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A memory-only marker would rebuy whole-pool verification after every
    restart."""
    fake = mock_call_llm_json(monkeypatch, leaf, make_verification_response())

    h = _finalist("verified before the restart")
    await dv.deep_verification_node(make_state(hypotheses=[h]))
    assert fake.await_count == 1

    restored = Hypothesis.from_dict(h.to_dict())
    await dv.deep_verification_node(make_state(hypotheses=[restored]))

    assert fake.await_count == 1


async def test_a_failed_verification_spends_the_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failures consume issuance too; lower retry budgets answer transient
    failures."""
    calls = 0

    async def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("verifier unavailable")

    monkeypatch.setattr(leaf, "call_llm_json", _boom)

    h = _finalist("leader", elo_rating=2000)
    state = make_state(hypotheses=[h])
    await dv.deep_verification_node(state)
    await dv.deep_verification_node(state)

    assert calls == 1
    assert h.deep_verification_fingerprint is None
    assert h.deep_verification_verdict == dv.VERDICT_UNVERIFIED


async def test_blocked_ideas_are_not_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mock_call_llm_json(monkeypatch, leaf, make_verification_response())
    blocked = make_hypothesis(text="blocked", review_disposition="inaccurate")

    await dv.deep_verification_node(make_state(hypotheses=[blocked]))

    assert fake.await_count == 0
    assert not dv.verification_issued(blocked)


async def test_verification_grounds_probes_in_corpus_when_mcp_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {
        "probes": [
            {
                "question": "Does sertraline dissipate proton motive force?",
                "answer": "Unknown.",
                "reasoning": "The corpus does not resolve it.",
                "assumption_is_fundamental": True,
                "search_query": "sertraline proton motive force",
            }
        ],
        "verdict": "weakened",
        "overall_assessment": "Evidence is missing.",
    }
    final = dict(first, verdict="holds")
    call = AsyncMock(side_effect=[first, final])
    monkeypatch.setattr(leaf, "call_llm_json", call)

    state = make_state(
        hypotheses=[_finalist("leader", elo_rating=2000)],
        articles=[
            make_article(
                "Sertraline membrane study",
                abstract="sertraline proton motive force bacterial membrane",
                used_in_analysis=True,
            ),
            make_article(
                "Unrelated ecology survey",
                abstract="soil microbiome diversity survey",
                used_in_analysis=True,
            ),
            make_article(
                "Retracted sertraline study",
                abstract="sertraline proton motive force bacterial membrane",
                is_retracted=True,
            ),
        ],
        research_goal="goal",
        model_name="test/model",
        run_id="r1",
        mcp_available=False,
    )

    output = await dv.deep_verification_node(state)

    assert call.await_count == 2
    second_prompt = call.await_args_list[1].kwargs["prompt"]
    assert "Targeted probe evidence" in second_prompt
    probe_evidence = second_prompt.split("Targeted probe evidence", 1)[1]
    assert "Sertraline membrane study" in probe_evidence
    assert "Unrelated ecology survey" not in probe_evidence
    assert "Retracted sertraline study" not in second_prompt
    result_errors = output["hypotheses"][0].enrichments["deep_verification"]["retrieval_errors"]
    assert dve.CORPUS_FALLBACK_NOTE in result_errors
    assert output["hypotheses"][0].deep_verification_verdict == "holds"
