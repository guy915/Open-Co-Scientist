from __future__ import annotations

from co_scientist.domains.research_state.models import (
    ExecutionMetrics,
    Hypothesis,
    HypothesisOrigin,
)


def test_multi_parent_lineage_records_every_parent() -> None:
    primary = Hypothesis(text="primary parent")
    partner = Hypothesis(text="partner parent")
    child = Hypothesis(
        text="combined child",
        parent_id=primary.id,
        parent_ids=[primary.id, partner.id],
        generation=1,
        origin=HypothesisOrigin.EVOLUTION,
    )
    assert child.parent_id == primary.id
    assert child.parent_ids == [primary.id, partner.id]
    restored = Hypothesis.from_dict(child.to_dict())
    assert restored.parent_id == primary.id
    assert restored.parent_ids == [primary.id, partner.id]


def test_hypothesis_lineage_round_trips() -> None:
    child = Hypothesis(
        text="child",
        parent_id="parent-123",
        generation=2,
        origin=HypothesisOrigin.EVOLUTION,
        creation_iteration=3,
    )
    d = child.to_dict()
    assert d["parent_id"] == "parent-123"
    assert d["generation"] == 2
    assert d["origin"] == "evolution"
    assert d["creation_iteration"] == 3
    restored = Hypothesis.from_dict(d)
    assert restored.parent_id == "parent-123"
    assert restored.generation == 2
    assert restored.origin is HypothesisOrigin.EVOLUTION
    assert restored.creation_iteration == 3


def test_execution_metrics_round_trips_model_usage() -> None:
    usage = {"generate::m": {"calls": 2, "prompt_tokens": 30}}
    m = ExecutionMetrics(model_usage=usage)
    restored = ExecutionMetrics.from_dict(m.to_dict())
    assert restored.model_usage == usage
