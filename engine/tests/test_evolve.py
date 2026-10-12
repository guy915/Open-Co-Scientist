from __future__ import annotations

from typing import Any

import pytest

from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.science.evolution import EvolutionContext, evolve
from co_scientist.science.evolution.evolve import evolve_single_hypothesis
from co_scientist.science.evolution.evolve_prompt import (
    EvolutionOperator,
    _EvolutionOperation,
    operator_instruction,
    operator_template,
)
from tests._llm_fake import stub_call_llm_json
from tests._state import make_hypothesis

# Disjoint vocabulary avoids unchanged and near-duplicate guards.
_RAPAMYCIN_RESPONSE: dict[str, Any] = {
    "hypothesis": "rapamycin suppresses mtor signaling downstream",
    "explanation": "fresh layman walkthrough",
    "experiment": "knock down the kinase and measure growth",
    "refinement_summary": "pivoted to a kinase mechanism",
}


def _children(result: dict[str, Any]) -> list[Hypothesis]:
    return list(result["hypotheses"].items)


def _operator_child_payload(operator: EvolutionOperator) -> dict[str, Any]:
    return {
        "hypothesis": (
            f"The {operator.value} route tests a distinct temporal "
            "checkpoint with an orthogonal perturbation and readout."
        ),
        "explanation": "The operator creates a separately testable path.",
        "experiment": "Perturb the checkpoint and compare the readout.",
        "refinement_summary": f"Applied {operator.value} behavior.",
    }


@pytest.mark.parametrize("operator", list(EvolutionOperator))
async def test_every_operator_executes_as_a_distinct_evolution_task(
    monkeypatch: pytest.MonkeyPatch,
    operator: EvolutionOperator,
) -> None:
    calls = stub_call_llm_json(
        monkeypatch,
        evolve,
        _operator_child_payload(operator),
        copy_response=True,
    )
    parent = make_hypothesis("A parent proposal links metabolic state to recovery kinetics.")

    child, detail = await evolve_single_hypothesis(
        parent,
        other_hypotheses=[],
        context=EvolutionContext(
            model_name="fake/model",
            meta_review={},
            removed_duplicates=[],
            creation_iteration=2,
        ),
        operation=_EvolutionOperation(operator=operator),
    )

    observed_prompt = calls[-1]["prompt"]
    assert child is not None
    assert detail is not None
    if operator_template(operator) == "evolution":
        assert f"**Operator:** {operator.value}" in observed_prompt
        assert operator_instruction(operator) in observed_prompt
    else:
        assert "## Required Evolution Operator" not in observed_prompt
    assert detail["operator"] == operator.value
    assert child.parent_id == parent.id
    assert child.parent_ids == [parent.id]
    assert child.generation == parent.generation + 1
    assert child.creation_iteration == 2
