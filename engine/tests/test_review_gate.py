from __future__ import annotations

import pathlib
from typing import Any

import pytest

from co_scientist.core.constants import NEEDS_REVISION_SCORE, NOT_VIABLE_SCORE
from co_scientist.domains.research_state.models import (
    SCIENTIST_REVIEWER,
    Hypothesis,
    HypothesisReview,
    has_peer_review,
)
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.science.prompts.loading import load_prompt_with_schema
from co_scientist.science.reflection.review import review_node
from co_scientist.science.reflection.review_gate import (
    ReviewType,
    _apply_initial_review_gate,
    _gate_axes_for_criteria,
    derive_review_disposition,
    finalist_review_needed,
    prompt_name_for,
    refresh_review_dispositions,
)
from co_scientist.science.scheduling.models import TaskType
from co_scientist.science.scheduling.policy import required_transition
from co_scientist.science.schemas import get_schema_for_prompt
from co_scientist.science.schemas.review import FULL_REVIEW_SCHEMA
from co_scientist.science.supervisor.orchestrator import (
    _compute_stats,
    _default_budget,
)
from tests._state import make_hypothesis, make_state


def _review_with_scores(scores: dict[str, int]) -> HypothesisReview:
    return HypothesisReview(
        review_summary="summary",
        scores=scores,
        safety_ethical_concerns="none",
        detailed_feedback={},
        constructive_feedback="feedback",
        overall_score=5,
    )


@pytest.mark.parametrize(
    ("criteria", "axes"),
    [
        (None, ("scientific_soundness", "novelty")),
        (
            ["purely aesthetic presentation"],
            ("scientific_soundness", "novelty"),
        ),
        (
            [
                "Scientific soundness",
                "Novelty over known mechanisms",
                "Discriminating experimental design",
                "Translational feasibility",
            ],
            (
                "scientific_soundness",
                "novelty",
                "testability",
                "potential_impact",
            ),
        ),
        (
            [
                "Idea correctness: Required",
                "Idea novelty: Required",
                "Maximize impact: Yes",
            ],
            ("scientific_soundness", "novelty", "potential_impact"),
        ),
    ],
    ids=["absent", "unmapped", "scientist_axes", "app_defaults"],
)
def test_criteria_resolve_onto_the_scored_axes_the_gate_checks(
    criteria: list[str] | None, axes: tuple[str, ...]
) -> None:
    assert _gate_axes_for_criteria(criteria) == axes


@pytest.mark.parametrize(
    ("scores", "disposition"),
    [
        ({"scientific_soundness": 8, "novelty": 8, "safety": 1}, "unsafe"),
        ({"scientific_soundness": 1, "novelty": 1, "safety": 1}, "unsafe"),
        ({"scientific_soundness": 8, "novelty": 8}, "viable"),
        ({"scientific_soundness": 8, "novelty": 8, "safety": 4}, "viable"),
    ],
    ids=[
        "serious",
        "outranks_soundness",
        "missing_is_no_verdict",
        "rework_band",
    ],
)
def test_only_a_serious_safety_score_blocks_the_idea(
    scores: dict[str, int], disposition: str
) -> None:
    hypothesis = make_hypothesis(text="idea")

    _apply_initial_review_gate([hypothesis], [_review_with_scores(scores)], criteria=None)

    assert hypothesis.review_disposition == disposition
    assert hypothesis.is_rankable() == (disposition == "viable")


@pytest.mark.parametrize(
    ("criteria", "disposition"),
    [(["Discriminating experimental design"], "inaccurate"), (None, "viable")],
)
def test_a_fatal_criterion_axis_blocks_where_the_default_gate_would_not(
    criteria: list[str] | None, disposition: str
) -> None:
    hypothesis = make_hypothesis(text="idea")
    review = _review_with_scores({"scientific_soundness": 8, "novelty": 8, "testability": 1})

    _apply_initial_review_gate([hypothesis], [review], criteria=criteria)

    assert hypothesis.review_disposition == disposition
    assert hypothesis.is_rankable() == (disposition == "viable")


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        (["Translational feasibility"], "needs_revision"),
        (["Scientific soundness"], "viable"),
    ],
    ids=["rework_band_on_criterion_axis", "strong_on_selected_axis"],
)
def test_criterion_axes_use_the_same_quality_bands(criteria: list[str], expected: str) -> None:
    hypothesis = make_hypothesis(text="idea")
    review = _review_with_scores(
        {
            "scientific_soundness": 8,
            "novelty": 8,
            "testability": 4,
            "potential_impact": 4,
        }
    )

    _apply_initial_review_gate([hypothesis], [review], criteria=criteria)

    assert hypothesis.review_disposition == expected


def test_canonical_axes_keep_their_disposition_names_under_criteria() -> None:
    unsound = make_hypothesis(text="unsound")
    stale = make_hypothesis(text="stale")
    review_unsound = _review_with_scores(
        {"scientific_soundness": 2, "novelty": 8, "testability": 8}
    )
    review_stale = _review_with_scores({"scientific_soundness": 8, "novelty": 1, "testability": 8})

    _apply_initial_review_gate(
        [unsound, stale],
        [review_unsound, review_stale],
        criteria=["Scientific soundness", "Novelty", "Testability"],
    )

    assert unsound.review_disposition == "inaccurate"
    assert stale.review_disposition == "non_novel"


def _gate_review(soundness: int, novelty: int) -> HypothesisReview:
    return HypothesisReview(
        review_summary="summary",
        scores={"scientific_soundness": soundness, "novelty": novelty},
        safety_ethical_concerns="none",
        detailed_feedback={},
        constructive_feedback="feedback",
        overall_score=5,
    )


def _review_disposition_revisable_scientist_review() -> HypothesisReview:
    return HypothesisReview(
        review_summary="[scientist-review:7] looks promising",
        scores={"scientist_assessment": 8},
        safety_ethical_concerns="",
        detailed_feedback={"scientist_verdict": "accept"},
        constructive_feedback="promising",
        overall_score=8.0,
    )


def _blocked_hypothesis(**overrides: Any) -> Hypothesis:
    hypothesis = make_hypothesis(reviews=[_gate_review(soundness=2, novelty=8)], **overrides)
    hypothesis.review_disposition = "inaccurate"
    return hypothesis


def _full_review_result(verdict: str) -> dict[str, Any]:
    return {"verdict": verdict, "justification": "because"}


def test_a_deeper_review_clears_the_initial_screen_block() -> None:
    hypothesis = _blocked_hypothesis()
    hypothesis.enrichments[ReviewType.FULL.value] = _full_review_result("sound")

    assert not hypothesis.is_rankable()
    refresh_review_dispositions([hypothesis])

    assert hypothesis.review_disposition == "viable"
    assert hypothesis.is_rankable()


def test_a_deeper_review_can_still_block() -> None:
    hypothesis = make_hypothesis(reviews=[_gate_review(8, 8)])
    hypothesis.review_disposition = "viable"
    hypothesis.enrichments[ReviewType.FULL.value] = _full_review_result("rejected")

    refresh_review_dispositions([hypothesis])

    assert hypothesis.review_disposition == "inaccurate"
    assert not hypothesis.is_rankable()


def test_a_narrower_later_review_does_not_undo_a_fatal_one() -> None:
    """A mechanism simulation cannot overturn correctness rejected by the
    full review."""
    hypothesis = make_hypothesis(reviews=[_gate_review(8, 8)])
    hypothesis.enrichments[ReviewType.FULL.value] = _full_review_result("rejected")
    hypothesis.enrichments[ReviewType.SIMULATION.value] = {"verdict": "holds"}

    refresh_review_dispositions([hypothesis])

    assert hypothesis.review_disposition == "inaccurate"


def test_a_correctness_verdict_does_not_clear_a_safety_block() -> None:
    """Scientific correctness says nothing about an independent safety
    concern."""
    hypothesis = make_hypothesis(reviews=[_gate_review(8, 8)])
    hypothesis.reviews[0].scores["safety"] = 1
    hypothesis.review_disposition = "unsafe"
    hypothesis.enrichments[ReviewType.FULL.value] = _full_review_result("sound")

    refresh_review_dispositions([hypothesis])

    assert hypothesis.review_disposition == "unsafe"
    assert not hypothesis.is_rankable()


def test_a_review_scoring_no_gated_axis_does_not_decide_the_gate() -> None:
    """A human assessment on an unrelated axis cannot clear existing blocks
    through neutral defaults."""
    hypothesis = _blocked_hypothesis()
    hypothesis.reviews.append(_review_disposition_revisable_scientist_review())

    refresh_review_dispositions([hypothesis])

    assert hypothesis.review_disposition == "inaccurate"


def test_the_most_recent_gated_review_wins_over_the_first() -> None:
    hypothesis = _blocked_hypothesis()
    hypothesis.reviews.append(_gate_review(soundness=9, novelty=8))

    assert derive_review_disposition(hypothesis) == "viable"


@pytest.mark.parametrize("foreign", ["evidence_blocked", "review_failed", "duplicate"])
def test_dispositions_owned_elsewhere_are_left_alone(foreign: str) -> None:
    """Evidence gates, failed calls and proximity own their separate
    dispositions."""
    hypothesis = make_hypothesis(reviews=[_gate_review(8, 8)])
    hypothesis.review_disposition = foreign

    refresh_review_dispositions([hypothesis])

    assert hypothesis.review_disposition == foreign


@pytest.mark.asyncio
async def test_review_node_revisits_dispositions_with_nothing_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hypothesis = _blocked_hypothesis()
    hypothesis.enrichments[ReviewType.FULL.value] = _full_review_result("sound")
    state = make_state(hypotheses=[hypothesis])

    async def _fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("review_node must not call the LLM here")

    monkeypatch.setattr("co_scientist.science.reflection.review.call_llm_json", _fail)

    await review_node(state)

    assert hypothesis.is_rankable()


def test_evolution_children_re_enter_review_unreviewed() -> None:
    from co_scientist.science.evolution.evolve_results import (
        _build_evolution_child,
        _RefinedFields,
    )

    parent = _blocked_hypothesis()
    child = _build_evolution_child(
        [parent],
        _RefinedFields(
            refined_text="a rewritten idea",
            explanation="why",
            experiment="how",
            refinement_summary="what changed",
            title="Rewritten",
        ),
        creation_iteration=1,
    )

    assert child.reviews == []
    assert child.review_disposition is None


def test_every_review_type_resolves_to_a_prompt_and_schema() -> None:
    for review_type in ReviewType:
        name = prompt_name_for(review_type)
        schema = get_schema_for_prompt(name)
        assert schema is not None, f"{review_type} has no schema"
        prompt, loaded_schema = load_prompt_with_schema(
            name,
            {
                "research_goal": "A goal",
                "hypothesis_text": "A hypothesis.",
                "domain_context": "",
                "tool_instructions": "",
            },
        )
        assert prompt.strip()
        assert loaded_schema == schema


def test_full_review_prompt_names_every_required_field() -> None:
    """Requiring an unnamed field buys repeated validation failures from
    prompt-faithful answers."""
    schema = get_schema_for_prompt(prompt_name_for(ReviewType.FULL))
    assert schema is not None
    template = load_prompt_with_schema(
        "full_review",
        {
            "research_goal": "A goal",
            "hypothesis_text": "A hypothesis.",
            "domain_context": "",
            "tool_instructions": "",
        },
    )[0]
    unnamed = [field for field in schema["schema"]["required"] if f"`{field}`" not in template]
    assert not unnamed, f"full_review.md does not name {unnamed}"


def test_full_review_prompt_names_every_reviews_summary_part() -> None:
    """Requiring an unnamed field buys repeated validation failures from
    prompt-faithful answers."""
    node = FULL_REVIEW_SCHEMA["schema"]["properties"]["reviews_summary"]
    template = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "co_scientist"
        / "science"
        / "prompts"
        / "templates"
        / "full_review.md"
    ).read_text()
    for part in node["properties"]:
        assert f"`{part}`" in template, part


def _agent_review(**scores: int) -> HypothesisReview:
    return HypothesisReview(
        review_summary="agent summary",
        scores={"scientific_soundness": 8, "novelty": 8, "safety": 9, **scores},
        safety_ethical_concerns="none",
        detailed_feedback={},
        constructive_feedback="feedback",
        overall_score=8,
    )


def _scientist_review_gate_scientist_review(score: int) -> HypothesisReview:
    return HypothesisReview(
        review_summary="[scientist-review:7] Scientist verdict",
        scores={"scientist_assessment": score},
        safety_ethical_concerns="",
        detailed_feedback={"scientist_verdict": "oppose"},
        constructive_feedback="the control cannot distinguish the mechanism",
        overall_score=float(score),
        reviewer=SCIENTIST_REVIEWER,
    )


def _hypothesis_with(*reviews: HypothesisReview) -> Hypothesis:
    hypothesis = make_hypothesis("A proposed mechanism for kinase X")
    hypothesis.reviews.extend(reviews)
    return hypothesis


def test_a_scientist_review_alone_is_not_a_peer_review() -> None:
    assert not has_peer_review(_hypothesis_with(_scientist_review_gate_scientist_review(2)))
    assert has_peer_review(_hypothesis_with(_agent_review()))
    assert has_peer_review(
        _hypothesis_with(_scientist_review_gate_scientist_review(2), _agent_review())
    )


def test_the_scheduler_still_counts_a_human_reviewed_idea_unreviewed() -> None:
    state = make_state()
    state["hypotheses"] = [_hypothesis_with(_scientist_review_gate_scientist_review(8))]

    stats = _compute_stats(state, {})

    assert stats.reviewed_count == 0
    assert stats.unreviewed_count == 1


def test_an_opposing_scientist_verdict_withholds_the_idea() -> None:
    hypothesis = _hypothesis_with(
        _agent_review(),
        _scientist_review_gate_scientist_review(NOT_VIABLE_SCORE),
    )

    assert refresh_review_dispositions([hypothesis]) == 1
    assert hypothesis.review_disposition == "inaccurate"
    assert not hypothesis.is_rankable()


def test_a_revise_verdict_demotes_without_withholding() -> None:
    hypothesis = _hypothesis_with(
        _agent_review(),
        _scientist_review_gate_scientist_review(NEEDS_REVISION_SCORE),
    )

    assert derive_review_disposition(hypothesis) == "needs_revision"
    assert hypothesis.is_rankable()


def test_a_supporting_verdict_clears_a_quality_block() -> None:
    hypothesis = _hypothesis_with(
        _agent_review(scientific_soundness=1),
        _scientist_review_gate_scientist_review(8),
    )

    assert derive_review_disposition(hypothesis) == "viable"


def test_a_supporting_verdict_cannot_clear_a_safety_block() -> None:
    hypothesis = _hypothesis_with(
        _agent_review(safety=1), _scientist_review_gate_scientist_review(8)
    )

    assert refresh_review_dispositions([hypothesis]) == 1
    assert hypothesis.review_disposition == "unsafe"
    assert not hypothesis.is_rankable()


def test_the_latest_scientist_verdict_wins() -> None:
    hypothesis = _hypothesis_with(
        _agent_review(),
        _scientist_review_gate_scientist_review(NOT_VIABLE_SCORE),
        _scientist_review_gate_scientist_review(8),
    )

    assert derive_review_disposition(hypothesis) == "viable"


def test_a_scientist_verdict_cannot_reopen_a_foreign_disposition() -> None:
    hypothesis = _hypothesis_with(_agent_review(), _scientist_review_gate_scientist_review(8))
    hypothesis.review_disposition = "evidence_blocked"

    assert refresh_review_dispositions([hypothesis]) == 0
    assert hypothesis.review_disposition == "evidence_blocked"


def _scheduler_pool() -> WorkflowState:
    settled = _hypothesis_with(_agent_review())
    settled.win_count, settled.loss_count = 1, 1
    admitted = _hypothesis_with(_scientist_review_gate_scientist_review(8))
    return make_state(
        hypotheses=[settled, admitted],
        current_iteration=1,
        max_iterations=5,
    )


def test_an_admitted_idea_does_not_pull_the_run_into_ranking() -> None:
    """The coverage floor precedes backlog; unreviewed admissions must not
    become rankable debt."""
    state = _scheduler_pool()

    decision = required_transition(_compute_stats(state, {}), _default_budget(state))

    assert decision is not None
    assert decision.next_task is TaskType.REFLECT


class TestFinalistDepth:
    def test_a_fresh_finalist_is_owed_its_review(self) -> None:
        assert finalist_review_needed(make_hypothesis(text="a"))

    @pytest.mark.parametrize("verdict", ["sound", "rejected", "unreviewed"])
    def test_a_recorded_review_is_never_repaid(self, verdict: str) -> None:
        hypothesis = make_hypothesis(text="a")
        hypothesis.enrichments["full"] = {"verdict": verdict}

        assert not finalist_review_needed(hypothesis)
