from __future__ import annotations

from typing import Any

import pytest

from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm.offline.llm import (
    DEFAULT_OFFLINE_MODEL,
    install_offline_router,
)
from co_scientist.science.citations import ReferenceIndex
from co_scientist.science.evolution import (
    EvolutionContext,
    build_evolution_context,
    evolve,
)
from co_scientist.science.evolution.evolve import (
    evolve_node,
    evolve_single_hypothesis,
)
from co_scientist.science.evolution.evolve_prompt import (
    EvolutionOperator,
    _build_evolution_prompt,
    _EvolutionOperation,
)
from co_scientist.science.evolution.evolve_results import _apply_evolution_result
from tests._llm_fake import stub_call_llm_json
from tests._mcp import isolate_offline_router
from tests._state import make_article, make_hypothesis, make_state
from tests.test_evolve import _RAPAMYCIN_RESPONSE, _children


def _observation(
    child_text: str,
    parent: Hypothesis,
    peers: list[Hypothesis],
    proximity_graph: dict[str, Any] | None = None,
    combination_partner: Hypothesis | None = None,
) -> tuple[Hypothesis | None, dict[str, Any] | None]:
    partners: tuple[Hypothesis, ...] = ()
    if combination_partner is not None:
        partners = (combination_partner,)
    operator = EvolutionOperator.COMBINATION if partners else EvolutionOperator.ENHANCEMENT
    context = EvolutionContext(
        model_name="fake/model",
        meta_review={},
        removed_duplicates=[],
        proximity_graph=proximity_graph,
    )
    operation = _EvolutionOperation(operator=operator, partners=partners)
    response: dict[str, Any] = {
        "hypothesis": child_text,
        "refinement_summary": "refined",
        "_evolution_operator": operator.value,
    }
    if partners:
        response["combined_partners"] = [1]
    return _apply_evolution_result(parent, response, peers, context, operation)


@pytest.mark.parametrize(("similarity", "accepted"), [(1.0, False), (0.6, True)])
def test_guard_rejects_only_at_the_proximity_edge_threshold(
    similarity: float, accepted: bool
) -> None:
    parent = make_hypothesis(text="parent mechanism about kinase signaling")
    peer = make_hypothesis(text="a disjoint wording entirely")
    graph = {"edges": [{"source": parent.id, "target": peer.id, "similarity": similarity}]}

    child, detail = _observation(
        "novel child wording sharing nothing lexically", parent, [peer], graph
    )

    assert (child is not None and detail is not None) is accepted


def test_guard_coverage_fallback_rejects_near_verbatim_child() -> None:
    parent = make_hypothesis(text="parent mechanism about kinase signaling")
    peer_text = "rapamycin suppresses mtor signaling downstream of the kinase"
    peer = make_hypothesis(text=peer_text)

    child, _ = _observation("rapamycin suppresses mtor signaling downstream", parent, [peer])

    assert child is None


def test_guard_coverage_is_not_dominated_by_a_short_peer() -> None:
    """Coverage uses the child's tokens; union-based similarity penalizes
    short-peer containment."""
    parent = make_hypothesis(text="parent mechanism about kinase signaling")
    peer = make_hypothesis(text="rapamycin suppresses mtor")
    long_child = (
        "rapamycin suppresses mtor while an orthogonal readout of "
        "autophagic flux distinguishes cytostatic from cytotoxic "
        "responses across a dose matrix in patient-derived organoids"
    )

    child, detail = _observation(long_child, parent, [peer])

    assert child is not None and detail is not None


def test_guard_exempts_resolved_combination_partners() -> None:
    """A faithful combination must resemble its designated partners."""
    parent = make_hypothesis(text="parent mechanism about kinase signaling")
    partner = make_hypothesis(text="partner mechanism about proteolysis")
    outsider = make_hypothesis(text="an unrelated third idea altogether")

    child, detail = _observation(
        partner.text,
        parent,
        [partner, outsider],
        combination_partner=partner,
    )

    assert child is not None
    assert detail is not None


def test_combination_records_every_parent() -> None:
    parent = make_hypothesis(text="parent mechanism about kinase signaling")
    partner = make_hypothesis(
        text="partner mechanism about proteolysis",
        explanation="partner explanation",
        experiment="partner experiment",
    )
    response = {
        "hypothesis": "a merged mechanism covering both signaling routes",
        "refinement_summary": "combined",
        "_evolution_operator": "combination",
        "combined_partners": [1],
    }

    child, detail = _apply_evolution_result(
        parent,
        response,
        [],
        EvolutionContext(model_name="fake/model", meta_review={}, removed_duplicates=[]),
        _EvolutionOperation(operator=EvolutionOperator.COMBINATION, partners=(partner,)),
    )

    assert child is not None and detail is not None
    assert child.parent_id == parent.id
    assert child.parent_ids == [parent.id, partner.id]
    assert detail["parent_id"] == parent.id
    assert detail["parent_ids"] == [parent.id, partner.id]


def test_combination_invalid_indices_degrade_to_single_parent() -> None:
    parent = make_hypothesis(text="parent mechanism about kinase signaling")
    partner = make_hypothesis(text="partner mechanism about proteolysis")

    child, detail = _apply_evolution_result(
        parent,
        {
            "hypothesis": "a refined mechanism with no usable partner",
            "refinement_summary": "combined",
            "_evolution_operator": "combination",
            "combined_partners": [99],
        },
        [],
        EvolutionContext(model_name="fake/model", meta_review={}, removed_duplicates=[]),
        _EvolutionOperation(operator=EvolutionOperator.COMBINATION, partners=(partner,)),
    )

    assert child is not None and detail is not None
    assert child.parent_id == parent.id
    assert child.parent_ids == [parent.id]


async def test_enhancement_grounding_falls_back_to_run_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub_call_llm_json(monkeypatch, evolve, _RAPAMYCIN_RESPONSE, copy_response=True)
    parent = make_hypothesis(text="parent idea about oxidative stress")
    article = make_article(title="A run-accumulated evidence source")
    state = make_state(hypotheses=[parent], articles=[article])

    child, _ = await evolve_single_hypothesis(
        parent,
        other_hypotheses=[],
        context=EvolutionContext(
            model_name="fake/model",
            meta_review={},
            removed_duplicates=[],
            state=state,
        ),
    )

    observed_prompt = calls[-1]["prompt"]
    assert child is not None
    assert "A run-accumulated evidence source" in observed_prompt


async def test_enhancement_grounding_retrieves_when_mcp_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.science.evolution import evolve_grounding

    observed_prompt = ""
    observed_queries: list[str] = []

    async def fake_query_llm(*, prompt: str, **_: Any) -> dict[str, Any]:
        return {"queries": ["mtor", "rapamycin resistance"]}

    async def fake_retrieval(state: Any, queries: list[str]) -> tuple[list[Any], list[str]]:
        observed_queries.extend(queries)
        return [make_article(title="Freshly retrieved grounding source")], []

    async def fake_llm(*, prompt: str, **_: Any) -> dict[str, Any]:
        nonlocal observed_prompt
        observed_prompt = prompt
        return dict(_RAPAMYCIN_RESPONSE)

    monkeypatch.setattr(evolve_grounding, "call_llm_json", fake_query_llm)
    monkeypatch.setattr(evolve_grounding, "_retrieve_probe_evidence", fake_retrieval)
    monkeypatch.setattr(evolve, "call_llm_json", fake_llm)
    parent = make_hypothesis(text="parent idea about oxidative stress")
    state = make_state(hypotheses=[parent], mcp_available=True)

    child, _ = await evolve_single_hypothesis(
        parent,
        other_hypotheses=[],
        context=EvolutionContext(
            model_name="fake/model",
            meta_review={},
            removed_duplicates=[],
            state=state,
        ),
    )

    assert child is not None
    assert observed_queries == ["mtor", "rapamycin resistance"]
    assert "Freshly retrieved grounding source" in observed_prompt


def test_grounding_metrics_extra_counts_only_live_enhancements() -> None:
    from co_scientist.science.evolution.evolve_grounding import (
        grounding_metrics_extra,
    )

    operators = ["enhancement", "combination", "enhancement"]
    assert grounding_metrics_extra(make_state(mcp_available=True), operators) == 2
    assert grounding_metrics_extra(make_state(mcp_available=False), operators) == 0


async def test_evolution_prompt_splices_falsified_assumptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub_call_llm_json(monkeypatch, evolve, _RAPAMYCIN_RESPONSE, copy_response=True)
    parent = make_hypothesis(text="parent idea about oxidative stress")
    weakened = make_hypothesis(
        text="a weakened leader idea",
        deep_verification_verdict="weakened",
        deep_verification_probes=[
            {
                "question": "Is the cofactor present at all?",
                "answer": "No; it is absent in the relevant tissue.",
                "reasoning": "Measurements show no cofactor.",
                "assumption_is_fundamental": False,
            }
        ],
    )
    state = make_state(hypotheses=[parent, weakened])

    child, _ = await evolve_single_hypothesis(
        parent,
        other_hypotheses=[weakened],
        context=EvolutionContext(
            model_name="fake/model",
            meta_review={},
            removed_duplicates=[],
            state=state,
        ),
    )

    observed_prompt = calls[-1]["prompt"]
    assert child is not None
    assert "Assumptions Verified Incorrect" in observed_prompt
    assert "Is the cofactor present at all?" in observed_prompt


_PARENT_SECTIONS: dict[str, Any] = {
    "introduction": "Metabolic disease remains a major cause of morbidity.",
    "recent_findings": "Aldolase inhibitors have shown early promise [C1].",
    "literature_grounding": ("Quercetin depletes aldolase activity in hepatocytes [C1]."),
    "safety_and_toxicity": ("Quercetin is well tolerated at dietary doses in humans."),
}

_CHILD_SECTIONS: dict[str, Any] = {
    "introduction": "Growth signaling drives proliferative disease.",
    "recent_findings": "Kinase inhibitors reshaped the field [C2].",
    "literature_grounding": ("Rapamycin suppresses mtor signaling in mammalian cells [C2]."),
    "safety_and_toxicity": ("Rapamycin carries immunosuppression risk at chronic doses."),
}


def _evolved_parent() -> Any:
    return make_hypothesis(
        text="quercetin inhibits aldolase activity",
        citation_map={"C1": {"type": "paper", "title": "Aldolase"}},
        **_PARENT_SECTIONS,
    )


def _prompt_context(**overrides: Any) -> EvolutionContext:
    return EvolutionContext(
        model_name="test-model",
        meta_review={},
        removed_duplicates=[],
        state=make_state(),
        **overrides,
    )


async def test_child_sections_come_from_the_evolution_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _evolved_parent()
    state = make_state(hypotheses=[original], evolution_max_count=1)
    stub_call_llm_json(monkeypatch, evolve, {**_RAPAMYCIN_RESPONSE, **_CHILD_SECTIONS})

    child = _children(await evolve_node(state))[0]

    assert child.introduction == _CHILD_SECTIONS["introduction"]
    assert child.recent_findings == _CHILD_SECTIONS["recent_findings"]
    assert child.literature_grounding == (_CHILD_SECTIONS["literature_grounding"])
    assert child.safety_and_toxicity == (_CHILD_SECTIONS["safety_and_toxicity"])


@pytest.mark.parametrize("sections", [{}, dict.fromkeys(_CHILD_SECTIONS, "   ")])
async def test_child_never_inherits_the_parent_sections(
    monkeypatch: pytest.MonkeyPatch, sections: dict[str, str]
) -> None:
    """Parent sections can describe a molecule the child does not propose and
    falsely fail grounding."""
    original = _evolved_parent()
    state = make_state(hypotheses=[original], evolution_max_count=1)
    stub_call_llm_json(monkeypatch, evolve, {**_RAPAMYCIN_RESPONSE, **sections})

    child = _children(await evolve_node(state))[0]

    assert child.literature_grounding is None
    assert child.safety_and_toxicity is None
    assert child.introduction is None
    assert child.recent_findings is None


async def test_child_citation_map_resolves_its_own_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewritten grounding needs its own keys; the parent's map explains a
    different paragraph."""
    original = _evolved_parent()
    state = make_state(
        hypotheses=[original],
        evolution_max_count=1,
        articles=[
            make_article("Aldolase depletion", used_in_analysis=True),
            make_article("mTOR suppression", used_in_analysis=True),
        ],
    )
    stub_call_llm_json(monkeypatch, evolve, {**_RAPAMYCIN_RESPONSE, **_CHILD_SECTIONS})

    child = _children(await evolve_node(state))[0]

    assert list(child.citation_map) == ["C2"]
    assert child.citation_map["C2"]["title"] == "mTOR suppression"


async def test_offline_backend_produces_a_populated_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolate_offline_router(monkeypatch)
    install_offline_router()
    original = _evolved_parent()
    state = make_state(
        hypotheses=[original],
        evolution_max_count=1,
        model_name=DEFAULT_OFFLINE_MODEL,
    )

    child = _children(await evolve_node(state))[0]

    assert child.text
    for section in _PARENT_SECTIONS:
        value = getattr(child, section)
        assert value, f"offline child has no {section}"
        assert value != _PARENT_SECTIONS[section]


@pytest.mark.parametrize(
    "operator",
    [
        EvolutionOperator.ENHANCEMENT,
        EvolutionOperator.COHERENCE_FEASIBILITY,
        EvolutionOperator.OUT_OF_BOX,
    ],
)
def test_evolution_prompt_carries_the_citation_reference_list(
    operator: EvolutionOperator,
) -> None:
    """Without supplied reference keys, a child can invent unresolvable
    citations."""
    prompt, _ = _build_evolution_prompt(
        make_hypothesis(text="the parent hypothesis"),
        ["a peer hypothesis"],
        _prompt_context(
            reference_index=ReferenceIndex(
                text="[C1] Smith et al., 2023 - Aldolase inhibition",
                sources={"C1": {"type": "paper", "title": "Aldolase"}},
            )
        ),
        _EvolutionOperation(operator=operator),
    )

    assert "[C1] Smith et al., 2023" in prompt
    assert "Literature Grounding (required)" in prompt
    assert "Safety and Toxicity (required)" in prompt
    assert "{{MISSING" not in prompt


_PARENTS = (
    "alpha membrane channel governs sodium",
    "bravo cytokine triggers inflammation cascade",
    "charlie enzyme catalyzes lipid breakdown",
)

# Disjoint vocabulary avoids unrelated unchanged and near-duplicate guards.
_EVOLVED = {
    _PARENTS[0]: "hotel peptide blocks vesicle fusion irreversibly",
    _PARENTS[1]: "india cofactor rescues folding intermediates rapidly",
    _PARENTS[2]: "juliet chaperone prevents aggregation of nascent chains",
}

_DOOMED = _PARENTS[1]


def _stub_one_failing_evolution(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempted: list[str] = []

    async def fake(*, prompt: str, **_: Any) -> dict[str, Any]:
        for original, evolved in _EVOLVED.items():
            if f"**Original Hypothesis:**\n{original}" not in prompt:
                continue
            attempted.append(original)
            if original == _DOOMED:
                raise RuntimeError("provider refused this refinement")
            return {
                "hypothesis": evolved,
                "refinement_summary": f"refined: {evolved}",
            }
        raise AssertionError("evolution called for an unselected hypothesis")

    monkeypatch.setattr(evolve, "call_llm_json", fake)
    return attempted


async def test_one_failed_parent_leaves_its_siblings_evolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hypotheses = [make_hypothesis(text=text) for text in _PARENTS]
    state = make_state(hypotheses=hypotheses, evolution_max_count=3)
    attempted = _stub_one_failing_evolution(monkeypatch)

    result = await evolve_node(state)

    assert sorted(attempted) == sorted(_PARENTS)
    children = list(result["hypotheses"].items)
    evolved_texts = sorted(child.text for child in children)
    assert evolved_texts == sorted(
        text for original, text in _EVOLVED.items() if original != _DOOMED
    )
    assert len(result["evolution_details"]) == 2


def _state() -> WorkflowState:
    return make_state(
        hypotheses=[
            make_hypothesis(text="Selected parent mechanism", elo_rating=900),
            make_hypothesis(text="Unrelated sibling mechanism", elo_rating=1500),
        ],
        research_goal="Measure the parent mechanism",
        preferences="Use falsifiable interventions",
        lab_constraints=["Only cell culture"],
        current_iteration=4,
        meta_review={"common_weaknesses": ["Unrelated meta-review signal"]},
        supervisor_guidance={
            "workflow_plan": {
                "evolution_phase": {"iteration_strategy": "Unrelated supervisor signal"}
            }
        },
        removed_duplicates=[{"text": "Unrelated removed duplicate"}],
        run_setup_guidance="Use the supplied setup",
        run_focus_guidance="Test a narrow causal question",
        articles_with_reasoning="Analyzed literature evidence",
        articles=[
            make_article(title="Retained paper", used_in_analysis=True, year=2025),
            make_article(title="Unread paper", used_in_analysis=False),
        ],
        context_enrichment_sources=[
            {"display": "Retained knowledge source", "tool_id": "source-tool"}
        ],
        proximity_graph={"edges": []},
    )


def _assert_retained_evidence(context: EvolutionContext, state: WorkflowState) -> None:
    assert context.model_name == state["model_name"]
    assert context.creation_iteration == 4
    assert context.run_id == state["run_id"]
    assert context.tool_registry is state["tool_registry"]
    assert context.proximity_graph is state["proximity_graph"]
    assert context.run_setup_guidance == state["run_setup_guidance"]
    assert context.run_focus_guidance == state["run_focus_guidance"]
    assert context.articles_with_reasoning == state["articles_with_reasoning"]
    assert context.reference_index is not None
    assert list(context.reference_index.sources) == ["C1", "C2"]
    assert context.reference_index.sources["C1"]["title"] == "Retained paper"
    assert context.reference_index.sources["C2"]["tool_id"] == "source-tool"


def test_round_context_ranks_full_pool_and_retains_reference_keys() -> None:
    state = _state()
    parent, sibling = state["hypotheses"]
    duplicates = ["Unrelated removed duplicate"]
    guidance = state["supervisor_guidance"]
    context = build_evolution_context(state, duplicates, guidance)

    assert context.state is state
    assert context.ranked_hypotheses == (sibling, parent)
    assert context.meta_review is state["meta_review"]
    assert context.removed_duplicates is duplicates
    assert context.supervisor_guidance is guidance
    _assert_retained_evidence(context, state)
