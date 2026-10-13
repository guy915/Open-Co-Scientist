from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

import co_scientist.science.reflection.review_gate as review_recheck
from co_scientist.core.constants import COMPARATIVE_BATCH_THRESHOLD
from co_scientist.domains.research_state.models import Hypothesis, HypothesisReview
from co_scientist.science.reflection import comprehensive_reflection, review
from co_scientist.science.reflection.comprehensive_reflection import (
    comprehensive_reflection_node,
)
from co_scientist.science.reflection.review import (
    _sanitize_review_scores,
    review_node,
)
from co_scientist.science.reflection.review_gate import (
    RECHECK_REVIEW_TYPE,
    ReviewType,
    recheck_targets,
    store_mature_review_result,
)
from tests._llm_fake import stub_call_llm_json
from tests._state import make_hypothesis, make_review, make_state


def _review(soundness: int | None, novelty: int | None) -> HypothesisReview:
    scores: dict[str, int] = {}
    if soundness is not None:
        scores["scientific_soundness"] = soundness
    if novelty is not None:
        scores["novelty"] = novelty
    return make_review(
        review_summary="summary",
        scores=scores,
        safety_ethical_concerns="none",
        detailed_feedback={},
        constructive_feedback="feedback",
        overall_score=5,
    )


@pytest.mark.parametrize(
    ("soundness", "novelty", "expected"),
    [
        (2, 8, "inaccurate"),
        (8, 2, "non_novel"),
        (3, 8, "needs_revision"),
        (8, 4, "needs_revision"),
        (5, 5, "viable"),
        (2, 2, "inaccurate_and_non_novel"),
        (None, 8, "viable"),
    ],
    ids=[
        "not_viable_soundness_blocks",
        "not_viable_novelty_blocks",
        "rework_soundness_still_ranks",
        "rework_novelty_still_ranks",
        "moderate_is_viable",
        "both_fatal",
        "missing_score_is_a_parse_defect",
    ],
)
def test_only_the_non_viable_band_blocks_the_tournament(
    soundness: int | None, novelty: int, expected: str
) -> None:
    """Comparative reviews must spread scores; that does not imply fatal
    defects."""
    hypothesis = make_hypothesis(text="idea")

    review._apply_initial_review_gate([hypothesis], [_review(soundness, novelty)])

    assert hypothesis.review_disposition == expected
    assert hypothesis.is_rankable() == (expected in {"viable", "needs_revision"})


def _batch_entry(scores: dict[str, int]) -> dict[str, Any]:
    return {
        "review_summary": "batch summary",
        "scores": scores,
        "safety_ethical_concerns": "none noted",
        "detailed_feedback": {"novelty": "ok"},
        "constructive_feedback": "tighten the experiment",
    }


async def test_review_node_gates_on_the_run_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scores = {
        "scientific_soundness": 9,
        "novelty": 9,
        "testability": 1,
    }
    stub_call_llm_json(monkeypatch, review, {"reviews": [_batch_entry(scores)]})
    with_criteria = make_hypothesis(text="idea")
    without_criteria = make_hypothesis(text="idea")

    await review_node(
        state=make_state(
            hypotheses=[with_criteria],
            criteria=["Discriminating experimental design"],
        )
    )
    await review_node(state=make_state(hypotheses=[without_criteria]))

    assert with_criteria.review_disposition == "inaccurate"
    assert not with_criteria.is_rankable()
    assert without_criteria.review_disposition == "viable"
    assert without_criteria.is_rankable()


def test_out_of_range_scores_are_dropped_not_clamped() -> None:
    """Clamping a malformed score to the floor would create a fatal verdict."""
    sanitized = _sanitize_review_scores(
        {
            "scientific_soundness": 0,
            "novelty": 12,
            "relevance": "high",
            "testability": True,
            "safety": 8,
        }
    )
    assert sanitized == {"safety": 8}


async def test_parallel_individual_isolates_a_failed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def flaky(**kwargs: Any) -> dict[str, Any]:
        if "poisoned" in str(kwargs["prompt"]):
            raise RuntimeError("provider error")
        return {
            "review_summary": "individual summary",
            "scores": {"novelty": 6},
            "safety_ethical_concerns": "",
            "detailed_feedback": {},
            "constructive_feedback": "",
        }

    monkeypatch.setattr(review, "call_llm_json", flaky)
    count = COMPARATIVE_BATCH_THRESHOLD + 1
    hyps = [make_hypothesis(text="poisoned idea" if i == 2 else f"idea {i}") for i in range(count)]

    result = await review_node(state=make_state(hypotheses=hyps))

    reviewed = [h for h in result["hypotheses"] if h.reviews]
    assert len(reviewed) == count - 1
    assert result["hypotheses"][2].reviews == []
    assert result["metrics"].reviews_count == count - 1
    assert result["messages"][0]["metadata"]["review_failures"] == 1


def _blocked(index: int) -> Hypothesis:
    return make_hypothesis(
        text=f"blocked idea {index}",
        reviews=[make_review(scores={"scientific_soundness": 2, "novelty": 8})],
        review_disposition="inaccurate",
    )


def _viable(index: int) -> Hypothesis:
    return make_hypothesis(text=f"viable idea {index}", review_disposition="viable")


def _incident_pool() -> list[Hypothesis]:
    return [_blocked(i) for i in range(20)] + [_viable(i) for i in range(2)]


class _ReviewStub:
    def __init__(self, verdict: str | None) -> None:
        self.verdict = verdict
        self.calls: Counter[tuple[str, str]] = Counter()

    async def __call__(self, state: Any, hypothesis: Hypothesis, review_type: ReviewType) -> Any:
        self.calls[(review_type.value, hypothesis.id)] += 1
        result = None if self.verdict is None else {"verdict": self.verdict}
        return comprehensive_reflection._ReviewRun(review_type, result, None)

    def rechecks(self, hypotheses: list[Hypothesis]) -> int:
        return sum(
            self.calls[(RECHECK_REVIEW_TYPE.value, hypothesis.id)] for hypothesis in hypotheses
        )


def _stub_reviews(monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> _ReviewStub:
    stub = _ReviewStub(verdict)
    monkeypatch.setattr(comprehensive_reflection, "review_hypothesis", stub)
    return stub


def _state(hypotheses: list[Hypothesis]) -> Any:
    return make_state(hypotheses=hypotheses, articles_with_reasoning=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("verdict", "cleared"), [("sound", True), ("rejected", False)])
async def test_a_recheck_is_issued_once_whatever_it_finds(
    monkeypatch: pytest.MonkeyPatch, verdict: str, cleared: bool
) -> None:
    pool = _incident_pool()
    blocked = pool[:20]
    stub = _stub_reviews(monkeypatch, verdict)
    state = _state(pool)

    await comprehensive_reflection_node(state)
    assert stub.rechecks(blocked) == 20
    stub.calls.clear()
    await comprehensive_reflection_node(state)

    assert stub.rechecks(blocked) == 0
    assert all(h.is_rankable() for h in blocked) is cleared


@pytest.mark.asyncio
async def test_a_failed_recheck_call_still_spends_its_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = [_blocked(0)]
    stub = _stub_reviews(monkeypatch, None)
    state = _state(pool)

    await comprehensive_reflection_node(state)
    await comprehensive_reflection_node(state)

    assert stub.rechecks(pool) == 1


def test_the_recheck_marker_survives_a_checkpoint_round_trip() -> None:
    hypothesis = _blocked(0)
    review_recheck.mark_recheck_issued(hypothesis)

    restored = Hypothesis.from_dict(hypothesis.to_dict())

    assert recheck_targets([restored]) == []


def test_the_run_wide_ceiling_bounds_a_pathological_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_recheck, "MAX_RECHECKS_PER_RUN", 3)
    pool = _incident_pool()

    first = recheck_targets(pool)
    for hypothesis in first:
        review_recheck.mark_recheck_issued(hypothesis)
    second = recheck_targets(pool)

    assert len(first) == 3
    assert second == []


def _full_review(verdict: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "correctness": "checked",
        "assumptions": [],
        "quality_and_novelty": "fine",
        "literature_grounding": "grounded",
        "verdict": verdict,
        "justification": "because",
    }
    result.update(overrides)
    return result


def _simulation_review(verdict: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "model": "the model",
        "steps": [],
        "failure_points": [],
        "robustness": "robust",
        "verdict": verdict,
        "decisive_step": "step three",
    }
    result.update(overrides)
    return result


def _store(
    hypothesis: Hypothesis,
    review_type: ReviewType,
    result: dict[str, object],
    iteration: int = 1,
) -> None:
    store_mature_review_result(hypothesis, review_type, result, iteration)


@pytest.mark.parametrize(
    ("review_type", "result", "expected"),
    [
        (ReviewType.FULL, _full_review("rejected"), "inaccurate"),
        (ReviewType.RECURRENT, _full_review("rejected"), "inaccurate"),
        (
            ReviewType.SIMULATION,
            _simulation_review("breaks_down"),
            "inaccurate",
        ),
        (ReviewType.FULL, _full_review("sound"), "viable"),
        (ReviewType.SIMULATION, _simulation_review("holds"), "viable"),
        (
            ReviewType.SIMULATION,
            _simulation_review("partially_holds"),
            "viable",
        ),
        (
            ReviewType.FULL,
            _full_review(
                "sound",
                go_no_go_recommendation="No-Go — do not pursue.",
                time_to_verdict="Short",
            ),
            "viable",
        ),
        (
            ReviewType.FULL,
            _full_review("needs_revision"),
            "needs_revision",
        ),
    ],
    ids=[
        "fatal_full_review",
        "fatal_recurrent_review",
        "breaking_simulation",
        "sound_full_review",
        "holding_simulation",
        "partial_simulation_still_ranks",
        "go_no_go_is_display_only",
        "needs_revision_publishes_but_leaves_cascade",
    ],
)
def test_only_the_not_viable_band_blocks(
    review_type: ReviewType, result: dict[str, object], expected: str
) -> None:
    hypothesis = make_hypothesis(text="idea", review_disposition="viable")

    _store(hypothesis, review_type, result)

    assert hypothesis.review_disposition == expected
    assert hypothesis.is_rankable() == (expected != "inaccurate")
