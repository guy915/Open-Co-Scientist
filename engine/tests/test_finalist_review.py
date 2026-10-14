from __future__ import annotations

from typing import Any

import pytest

from co_scientist.core.exceptions import LLMCallBudgetExceededError
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.orchestration.engine_tasks.fanout_aggregates import _apply_one_reflection_item
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.prompts.loading import load_prompt_with_schema
from co_scientist.science.reflection import comprehensive_reflection as cr
from co_scientist.science.reflection import deep_verification as dv
from co_scientist.science.reflection.review_gate import ReviewType
from tests._llm_fake import mock_call_llm_json
from tests._state import make_hypothesis, make_review, make_state, make_verification_response


def _finalist_answer(**overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "observation": {
            "reasoning": "would we see this... hypothesis: missing piece",
            "classification": "missing piece",
            "positive_observations": ["explains observation A"],
        },
        "full_review": {
            "verdict": "sound",
            "assumptions": [
                {"assumption": "X binds Y", "reasoning": "thin", "support": "uncertain"}
            ],
        },
        "simulation": {"verdict": "partially_holds", "failure_points": ["step 3 stalls"]},
        "verification_queries": ["X Y binding", "  pathway   X  "],
    }
    answer.update(overrides)
    return answer


def _finalist(text: str = "a finalist") -> Hypothesis:
    return make_hypothesis(
        text=text, reviews=[make_review()], win_count=1, review_disposition="viable"
    )


def test_each_part_is_stored_under_its_standalone_key() -> None:
    hypothesis = _finalist()
    answer = _finalist_answer(retrieved_articles=[], executed=True, execution_observations="ran")

    cr.store_finalist_review(hypothesis, answer, iteration=2)

    assert (hypothesis.reflection_notes or "").endswith("Classification: missing piece")
    assert hypothesis.enrichments["observation"]["positive_observations"] == [
        "explains observation A"
    ]
    assert hypothesis.enrichments["full"]["verdict"] == "sound"
    assert hypothesis.enrichments["full"]["retrieved_articles"] == []
    assert hypothesis.enrichments["simulation"]["executed"] is True
    assert hypothesis.enrichments["verification_queries"] == ["X Y binding", "pathway X"]
    assert hypothesis.review_disposition == "viable"


@pytest.mark.parametrize(
    "part,verdict",
    [("full_review", "rejected"), ("simulation", "breaks_down")],
)
def test_a_fatal_part_blocks_the_idea_as_its_own_review_would(part: str, verdict: str) -> None:
    hypothesis = _finalist()

    cr.store_finalist_review(hypothesis, _finalist_answer(**{part: {"verdict": verdict}}), 0)

    assert hypothesis.review_disposition == "inaccurate"


def test_a_missing_part_is_unreviewed_never_a_pass() -> None:
    hypothesis = _finalist()

    cr.store_finalist_review(hypothesis, _finalist_answer(simulation=None), 0)

    assert hypothesis.enrichments["simulation"]["verdict"] == "unreviewed"


def test_existing_observation_notes_are_kept() -> None:
    hypothesis = _finalist()
    hypothesis.reflection_notes = "earlier\n\nClassification: neutral"

    cr.store_finalist_review(hypothesis, _finalist_answer(), 0)

    assert hypothesis.reflection_notes == "earlier\n\nClassification: neutral"


@pytest.mark.parametrize("literature", [None, "Article 1: observation A."])
def test_the_prompt_fills_every_placeholder(literature: str | None) -> None:
    state = make_state(articles_with_reasoning=literature)
    variables = cr._prompt_variables(state, _finalist(), ReviewType.FULL, [], None)
    variables["articles_with_reasoning"] = literature or cr._NO_OBSERVATIONS_NOTE
    variables["domain_reflection_guidance"] = ""

    prompt, schema = load_prompt_with_schema("finalist_review", variables)

    assert "MISSING" not in prompt
    assert schema is not None
    assert set(schema["schema"]["required"]) == {
        "full_review",
        "simulation",
        "verification_queries",
    }


async def test_without_literature_the_observation_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call_llm_json(monkeypatch, cr, _finalist_answer())

    run = await cr.review_finalist(make_state(), _finalist())

    assert run.result is not None
    assert "observation" not in run.result


async def test_a_finalist_costs_one_review_call_and_is_not_refreshed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mock_call_llm_json(monkeypatch, cr, _finalist_answer())
    pool = [_finalist("leader"), _finalist("runner-up")]

    await cr.comprehensive_reflection_node(make_state(hypotheses=pool, current_iteration=1))
    await cr.comprehensive_reflection_node(make_state(hypotheses=pool, current_iteration=2))

    assert fake.await_count == 2
    assert all(h.enrichments["full"]["verdict"] == "sound" for h in pool)


async def test_a_failed_review_records_both_parts_unreviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call_llm_json(monkeypatch, cr, side_effect=RuntimeError("bad answer"))
    hypothesis = _finalist()

    await cr.comprehensive_reflection_node(make_state(hypotheses=[hypothesis]))

    assert hypothesis.enrichments["full"]["verdict"] == "unreviewed"
    assert hypothesis.enrichments["simulation"]["verdict"] == "unreviewed"


async def test_spend_exhaustion_propagates_from_the_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_call_llm_json(monkeypatch, cr, side_effect=LLMCallBudgetExceededError(2, 1))

    with pytest.raises(LLMCallBudgetExceededError):
        await cr.review_finalist(make_state(), _finalist())


def test_a_failed_durable_item_records_both_parts_unreviewed() -> None:
    hypothesis = _finalist()

    _apply_one_reflection_item(
        hypothesis, ReviewType.FINALIST, {"verdict": "unreviewed", "justification": "lost"}, 0
    )

    assert hypothesis.enrichments["full"]["justification"] == "lost"
    assert hypothesis.enrichments["simulation"]["verdict"] == "unreviewed"


async def test_verification_searches_the_review_queries_before_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mock_call_llm_json(monkeypatch, dv, make_verification_response())
    searched: list[list[str]] = []

    async def retrieve(_state: object, queries: list[str]) -> tuple[list[Article], list[str]]:
        searched.append(queries)
        return [Article(title="Binding assay", source_id="s1", abstract="X binds Y.")], []

    monkeypatch.setattr(dv, "_retrieve_probe_evidence", retrieve)
    hypothesis = _finalist()
    hypothesis.enrichments["verification_queries"] = ["X Y binding"]

    result = await dv.verify_hypothesis(make_state(), hypothesis)

    assert result is not None
    assert searched == [["X Y binding"]]
    assert fake.await_count == 1
    assert "Binding assay" in fake.call_args.kwargs["prompt"]
    assert result["verification_llm_calls"] == 1
    assert result["retrieval_queries"] == ["X Y binding"]


async def test_verification_without_review_queries_keeps_its_probe_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_verification_response(
        probes=[{"question": "q", "answer": "a", "reasoning": "r", "search_query": "X Y"}]
    )
    fake = mock_call_llm_json(monkeypatch, dv, probe)

    async def retrieve(_state: object, _queries: list[str]) -> tuple[list[Article], list[str]]:
        return [Article(title="Probe paper", source_id="p1", abstract="evidence")], []

    monkeypatch.setattr(dv, "_retrieve_probe_evidence", retrieve)

    result = await dv.verify_hypothesis(make_state(), _finalist())

    assert result is not None
    assert fake.await_count == 2
    assert result["verification_llm_calls"] == 2
