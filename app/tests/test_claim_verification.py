from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from co_scientist.domains.report import content as report_content
from co_scientist.domains.report import gates as report_gates
from co_scientist.domains.report.content import derive_knowledge_facts
from co_scientist.domains.report.markdown.hypothesis import _render_claim_evidence
from co_scientist.domains.research_state.claims import (
    AssessorDraft,
    EntailmentLabel,
    EvidencePassage,
    as_passages,
    assess_claim,
)
from co_scientist.domains.research_state.claims.gate import ClaimEdge
from co_scientist.domains.research_state.claims.grounding import (
    AssessorSpec,
    assess_hypothesis_claims,
    persist_grounding,
)
from co_scientist.domains.research_state.repository import hypotheses
from co_scientist.platform.db import runs

from tests._client import create_run as _create_run
from tests._client import make_client
from tests._drain_helpers import _build_report
from tests._store_helpers import _add

# Partial supports the publication badge but is not a durable knowledge fact.
# Speculative contradictions remain findings; missing labels are not excused by
# speculative roles.

_LABELS = ("supports", "partial", "contradicts", "insufficient", None)
_ROLES = ("categorical", "speculative", None)
_ODD = (("bogus", "categorical"), ("insufficient", "exotic"))
_CELLS = [(label, role) for label in _LABELS for role in _ROLES] + list(_ODD)

_SUPPORTED = {"supports", "partial"}
_REASON = "Evidence verification did not support every material claim."
_STATUS = {
    "supports": "Supported",
    "partial": "Partially supported",
    "contradicts": "Contradicted",
}


def _matrix_edge(label: str | None, role: str | None) -> dict[str, Any]:
    edge: dict[str, Any] = {
        "hypothesis_id": "h1",
        "claim": "IL-6 increases inflammation via STAT3 signaling.",
        "supporting": [{"evidence_id": "e-for", "quote": "q"}],
        "contradicting": [{"evidence_id": "e-against", "quote": "q"}],
    }
    if label is not None:
        edge["label"] = label
    if role is not None:
        edge["claim_role"] = role
    return edge


@pytest.mark.parametrize(("label", "role"), _CELLS)
def test_every_reader_agrees_on_what_a_label_and_role_mean(
    label: str | None, role: str | None
) -> None:
    row = _matrix_edge(label, role)
    edge = ClaimEdge.from_row(row)
    speculative = role == "speculative"
    excused = label == "insufficient" and speculative

    supported = report_gates._supported_hypothesis_ids([edge])
    assert supported == ({"h1"} if label in _SUPPORTED else set())

    withheld = report_gates.contradicted_hypothesis_ids("", None, claim_edges=[edge])
    assert withheld == ({"h1"} if label == "contradicts" and not speculative else set())

    panel = report_content._contradicted_claims([edge])
    assert len(panel) == (1 if label == "contradicts" else 0)

    reasons = report_content._claim_edge_reasons([edge])
    clean = label in _SUPPORTED or excused
    assert reasons == ({} if clean else {"h1": {_REASON}})

    expected_status = _STATUS.get(label or "")
    if expected_status is None:
        expected_status = (
            "Speculative — evidence insufficient"
            if speculative
            else "Unsupported categorical claim"
        )
    assert edge.status == expected_status
    line = _render_claim_evidence([row])[2]
    assert line.startswith(f"- **{expected_status} · {role or 'categorical'}**")

    topics = report_content._knowledge_base_topics([{"id": "h1", "title": "T"}], [edge])
    assert topics[0]["reference_ids"] == (["e-for"] if label in _SUPPORTED else [])

    facts = [(r["kind"], r["state"], r["evidence_id"]) for r in derive_knowledge_facts([edge])]
    assert facts == {
        "supports": [("fact", "supports", "e-for")],
        "contradicts": [("contradiction", "contradicts", "e-against")],
    }.get(label or "", [])


async def test_grounding_retains_method_across_api_reopen(
    isolated_db: str,
) -> None:
    client = make_client()
    run_id = _create_run(client, "Study tissue repair").json()["id"]
    claim = "A dietary change improves cardiovascular outcomes in adults."
    _add(run_id, "Diet study", claim, isolated_db)

    def assess(claim: str, passages: Sequence[EvidencePassage]) -> AssessorDraft:
        return AssessorDraft(
            EntailmentLabel.SUPPORTS,
            supporting=((passages[0].evidence_id, claim),),
            verification_method="model_primary",
        )

    persist_grounding(
        run_id,
        assess_hypothesis_claims(
            hypotheses.list_hypotheses(run_id),
            as_passages([claim]),
            AssessorSpec(assessor=assess, assessor_id="llm:test"),
        ),
    )
    reopened = make_client().get(f"/api/runs/{run_id}/claim-evidence")
    assert reopened.status_code == 200
    edges = reopened.json()["claim_evidence"]
    assert edges and all(e["verification_method"] == "model_primary" for e in edges)
    assert all(e["assessor"] == "llm:test" for e in edges)
    payload, markdown = await _build_report(runs.get_run(run_id), isolated_db)
    assert payload["claim_evidence"][0]["verification_method"] == "model_primary"
    assert "Assessment method: model judgment" in markdown


def test_no_candidates_cannot_claim_model_verification() -> None:
    def assess(_claim: str, _passages: Sequence[EvidencePassage]) -> AssessorDraft:
        return AssessorDraft(EntailmentLabel.INSUFFICIENT, verification_method="model_primary")

    result = assess_claim("A hypothesis without retrieved evidence.", [], assessor=assess)
    assert result.verification_method == "no_evidence"


def test_ideas_stating_one_claim_share_its_check_at_finalize() -> None:
    claim = "A dietary change improves cardiovascular outcomes in adults."
    checked: list[str] = []

    def assess(text: str, passages: Sequence[EvidencePassage]) -> AssessorDraft:
        checked.append(text)
        return AssessorDraft(EntailmentLabel.SUPPORTS, verification_method="model_primary")

    ideas = [
        {"id": "parent", "statement": claim},
        {"id": "child", "statement": claim, "mechanism": "Lower sodium reduces blood pressure."},
    ]

    result = dict(
        assess_hypothesis_claims(
            ideas, as_passages([claim]), AssessorSpec(assessor=assess, assessor_id="llm:test")
        )
    )

    assert sorted(checked) == sorted([claim, "Lower sodium reduces blood pressure."])
    assert [a.claim for a, _role in result["child"]] == [
        claim,
        "Lower sodium reduces blood pressure.",
    ]
    assert result["child"][0][0] is result["parent"][0][0]
