from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from co_scientist.domains.report import build as report_build
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.repository import hypotheses as store_hypotheses
from co_scientist.domains.research_state.repository import records
from co_scientist.orchestration.drain import fold_grounding_telemetry
from co_scientist.orchestration.engine_tasks import report_finalize
from co_scientist.platform import db
from co_scientist.platform.retrieval.article import Article
from co_scientist.science.citations import build_reference_index, resolve_citation_keys

from tests._client import drain as _drain
from tests._drain_helpers import (
    _build_report,
    _engine_hypothesis,
    _final_state_with_features,
    _final_state_with_lineage,
    _persist,
    _persist_and_finalize,
    emit_event,
)
from tests._store_helpers import seed_run


@pytest.mark.parametrize(
    ("guidance", "key", "expected"),
    [
        (None, "critical_criteria", []),
        (None, "attributes", []),
        (
            {"workflow_plan": {"review_phase": {"critical_criteria": ["a"]}}},
            "critical_criteria",
            ["a"],
        ),
        # Structured criteria are valid guidance; string-only filtering would
        # silently discard them.
        (
            {
                "workflow_plan": {
                    "review_phase": {
                        "critical_criteria": [
                            "novelty",
                            {"name": "Kinetic Feasibility", "questions": []},
                            42,
                            None,
                            ["not", "a", "criterion"],
                        ]
                    }
                }
            },
            "critical_criteria",
            ["novelty", {"name": "Kinetic Feasibility", "questions": []}],
        ),
        (
            {"workflow_plan": {"review_phase": "not a dict"}},
            "critical_criteria",
            [],
        ),
        (
            {"config_synthesis": {"attributes": [{"name": "Mechanism Novelty", "rubric": "1-5"}]}},
            "attributes",
            [{"name": "Mechanism Novelty", "rubric": "1-5"}],
        ),
        ({"config_synthesis": "not a dict"}, "attributes", []),
    ],
)
def test_drain_result_carries_supervisor_guidance_into_report_inputs(
    isolated_db: str,
    guidance: dict[str, Any] | None,
    key: str,
    expected: list[Any],
) -> None:
    run = seed_run("guidance goal")
    state = _final_state_with_features()
    if guidance is not None:
        state["supervisor_guidance"] = guidance

    drained = _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    assert drained.report_inputs[key] == expected


def _engine_review() -> dict[str, Any]:
    return {
        "review_summary": "Sound but narrow.",
        "scores": {
            "scientific_soundness": 8,
            "plausibility": 7,
            "novelty": 6,
            "testability": 9,
            "potential_impact": 5,
            "relevance": 4,
            "safety": 10,
            "clarity": 3,
        },
        "detailed_feedback": {
            "scientific_soundness": "The mechanism is internally consistent.",
            "novelty": "The pairing is unusual.",
            "testability": "A pilot assay would settle it.",
            "potential_impact": "Would change first-line practice.",
            "relevance": "Squarely on the research goal.",
            "clarity": "Precisely stated.",
        },
        "constructive_feedback": "Name the control arm.",
        "already_explored": ["Target engagement is documented."],
        "novel_aspects": ["The stress-induced modification is new."],
    }


def test_persist_writes_each_review_kind_as_its_own_row(
    isolated_db: str,
) -> None:
    hypothesis = _engine_hypothesis(
        "h-1",
        "A hypothesis.",
        reviews=[_engine_review()],
        deep_verification_probes=[
            {
                "question": "Does the receptor bind?",
                "answer": "Yes, at nanomolar affinity.",
                "reasoning": "Two structures show the contact.",
                "assumption_is_fundamental": True,
            },
            {"question": "", "answer": "dropped"},
        ],
        deep_verification_verdict="weakened",
        enrichments={
            "full": {
                "verdict": "rejected",
                "correctness": "The pathway claim is circular.",
                "justification": "Circular pathway reasoning.",
                "assumptions": [
                    {
                        "assumption": "The receptor is expressed.",
                        "reasoning": "Two cohorts detect it directly.",
                        "support": "supported",
                    },
                    {"assumption": "", "reasoning": "dropped"},
                ],
                "reviews_summary": {"conclusion": "Worth testing."},
                "feasibility_steps": ["Run the pilot."],
                "retrieved_articles": [{"title": "not persisted"}],
            },
            "simulation": {
                "verdict": "breaks_down",
                "failure_points": ["binding never occurs"],
            },
            "recurrent": {
                "verdict": "needs_revision",
                "justification": "Still circular after review.",
            },
        },
    )
    hypothesis["reviews"][0]["scores"]["vibes"] = 11
    run = seed_run("detail goal")
    _persist(
        run_id=run.id,
        final_state={
            "hypotheses": [hypothesis],
            "articles": [],
            "tournament_matchups": [],
            "meta_review": {},
            "evolution_details": [],
            "research_overview": {},
        },
        db_path=isolated_db,
    )

    rows = {row["reviewer_agent"]: row for row in records.list_reviews(run.id, db_path=isolated_db)}
    assert set(rows) == {
        "review",
        "deep_verification",
        "full_review",
        "simulation_review",
        "recurrent_review",
    }
    detail = {agent: json.loads(row["detail_json"] or "{}") for agent, row in rows.items()}
    review = detail["review"]
    assert review["scores"] == _engine_review()["scores"]
    assert len(review["detailed_feedback"]) == 6
    assert review["already_explored"] == ["Target engagement is documented."]
    assert review["novel_aspects"] == ["The stress-induced modification is new."]
    critique = rows["review"]["critique"]
    assert "Aspects already explored:" in critique
    assert "Novel Aspects:" in critique
    assert "Name the control arm." in critique

    deep = rows["deep_verification"]
    assert "Does the receptor bind?" in deep["critique"]
    assert (deep["novelty"], deep["overall"]) == (None, None)
    assert detail["deep_verification"]["probes"] == [
        {
            "question": "Does the receptor bind?",
            "answer": "Yes, at nanomolar affinity.",
            "reasoning": "Two structures show the contact.",
            "fundamental": True,
        }
    ]

    full = rows["full_review"]
    assert full["summary"] == "Full review verdict: rejected"
    assert "Circular pathway reasoning." in full["critique"]
    assert "The receptor is expressed." in full["critique"]
    assert detail["full_review"]["reviews_summary"] == {"conclusion": "Worth testing."}
    assert detail["full_review"]["assumptions"] == [
        {
            "assumption": "The receptor is expressed.",
            "reasoning": "Two cohorts detect it directly.",
            "support": "Plausible",
        }
    ]
    assert detail["full_review"]["feasibility_steps"] == ["Run the pilot."]
    assert "not persisted" not in str(rows)
    assert rows["simulation_review"]["summary"] == ("Simulation review verdict: breaks_down")
    assert "binding never occurs" in rows["simulation_review"]["critique"]
    assert rows["recurrent_review"]["summary"] == ("Recurrent review verdict: needs_revision")


@pytest.mark.parametrize(
    ("final_state", "usage", "calls"),
    [
        (
            {"metrics": {"llm_calls": 3, "model_usage": {}}},
            {"g::m": {"calls": 5}},
            8,
        ),
        ({}, {"g::m": {"calls": 2}}, 2),
        ({"metrics": {"llm_calls": 4}}, {}, 4),
    ],
)
def test_grounding_telemetry_is_charged_once_into_plain_metrics(
    final_state: dict[str, Any], usage: dict[str, Any], calls: int
) -> None:
    fold_grounding_telemetry(final_state, usage)

    metrics = final_state["metrics"]
    assert metrics["llm_calls"] == calls
    assert ("g::m" in metrics.get("model_usage", {})) is bool(usage)


def _assert_features_proximity_edge(run_id: str, db_path: str) -> None:
    with db.connect(db_path) as conn:
        edges = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM proximity_edges WHERE run_id=? ORDER BY id", (run_id,)
            )
        ]
    hypotheses = store_hypotheses.list_hypotheses(run_id, db_path=db_path)
    hypothesis_ids = {hypothesis["id"] for hypothesis in hypotheses}
    assert len(edges) == 1
    edge = edges[0]
    assert edge["source_hypothesis_id"] in hypothesis_ids
    assert edge["target_hypothesis_id"] in hypothesis_ids
    assert edge["source_hypothesis_id"] != edge["target_hypothesis_id"]
    assert edge["similarity"] == 0.82
    assert edge["degree"] == "high"
    assert edge["cluster_id"] == "cluster-1"
    assert edge["method"] == "llm_cluster_pairwise_graph"
    assert edge["version"] == "1"
    assert edge["model"] == "fixture-model"
    assert edge["updated_at"] == 1234.5


def _archived_parent_state() -> dict[str, Any]:
    state = _final_state_with_lineage()
    parent = state["hypotheses"][0]
    state["hypotheses"] = [state["hypotheses"][1]]
    state["removed_duplicates"] = [
        {
            "text": parent["text"],
            "cluster_id": "cluster-1",
            "reason": "high_similarity_duplicate",
            "kept_hypothesis_id": "child-1",
            "kept_instead": "Child hypothesis",
            "hypothesis": parent,
        }
    ]
    state["tournament_matchups"] = [
        {
            "hypothesis_a_id": "parent-1",
            "hypothesis_b_id": "child-1",
            "winner_id": "child-1",
            "reasoning": "The child is more specific.",
            "confidence": "High",
        }
    ]
    return state


def test_persist_writes_research_overview_into_report(isolated_db: str) -> None:
    run = seed_run("CSC goal")
    _persist_and_finalize(run, _final_state_with_features(), isolated_db)

    report = reports.get_latest_report(run.id, db_path=isolated_db)
    assert report is not None
    overview = report["payload"].get("research_overview")
    assert overview is not None
    assert overview["overview"]["summary"].startswith("Targeting CXCR1")
    first_aim = overview["nih_specific_aims"]["aims"][0]
    assert first_aim["overarching_goal"].startswith("Aim 1")

    markdown = report["markdown_text"]
    assert "## Research Overview" in markdown
    assert "Dual CXCR1/CXCR2 blockade" in markdown
    assert "Combine reparixin with a CXCR2 antagonist." in markdown
    assert "## NIH Specific Aims" in markdown
    assert "Aim 1: Quantify CXCR1 dependence." in markdown
    assert "Could yield a combination therapy for TNBC." in markdown

    evidence = records.list_evidence(run.id, db_path=isolated_db)
    retracted = next(item for item in evidence if item["title"] == "Retracted CXCR1 report")
    assert retracted["available"] == 0

    _assert_features_proximity_edge(run.id, isolated_db)


def test_drain_persists_explicit_lineage(isolated_db: str) -> None:
    # Lineage is explicit and append-only; do not infer it from evolution
    # history.
    run = seed_run("kinase goal")
    _persist(
        run_id=run.id,
        final_state=_final_state_with_lineage(),
        db_path=isolated_db,
    )

    hyps = store_hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    by_id = {h["id"]: h for h in hyps}
    assert set(by_id) == {"parent-1", "child-1"}

    parent = by_id["parent-1"]
    child = by_id["child-1"]
    assert parent["parent_id"] is None
    assert parent["generation"] == 0
    assert parent["created_by_agent"] == "generation"
    assert child["parent_id"] == "parent-1"
    assert child["generation"] == 1
    assert child["created_by_agent"] == "evolution"


def test_drain_drops_orphaned_parent_reference(isolated_db: str) -> None:
    # A pruned parent must not leave a dangling foreign key that aborts the
    # whole drain.
    state = _final_state_with_lineage()
    state["hypotheses"] = [h for h in state["hypotheses"] if h["id"] != "parent-1"]
    run = seed_run("kinase goal")
    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    hyps = store_hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    assert len(hyps) == 1
    assert hyps[0]["id"] == "child-1"
    assert hyps[0]["parent_id"] is None
    assert hyps[0]["generation"] == 1


def test_drain_preserves_proximity_pruned_parent_as_a_duplicate(
    isolated_db: str,
) -> None:
    # Duplicates were folded into peers without judgment; rejection would
    # misrepresent their science.
    state = _archived_parent_state()
    run = seed_run("kinase archive goal")

    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    by_id = {
        hypothesis["id"]: hypothesis
        for hypothesis in store_hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    }
    assert by_id["parent-1"]["status"] == "duplicate"
    assert by_id["child-1"]["parent_id"] == "parent-1"
    [match] = records.list_matches(run.id, db_path=isolated_db)
    assert match["winner_id"] == "child-1"
    assert match["loser_id"] == "parent-1"


@pytest.mark.parametrize(
    ("overrides", "status", "verdict"),
    [
        ({"review_disposition": "evidence_blocked"}, "rejected", None),
        # Undermined ideas still publish, so the verdict is what distinguishes
        # them from sound ideas.
        ({"deep_verification_verdict": "undermined"}, "active", "undermined"),
    ],
)
def test_drain_maps_evidence_outcomes_onto_publication_status(
    isolated_db: str,
    overrides: dict[str, Any],
    status: str,
    verdict: str | None,
) -> None:
    state = _final_state_with_lineage()
    state["hypotheses"][0].update(overrides)
    run = seed_run("archive goal")

    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    by_id = {
        hypothesis["id"]: hypothesis
        for hypothesis in store_hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    }
    assert by_id["parent-1"]["status"] == status
    assert by_id["parent-1"]["verification_verdict"] == verdict
    assert by_id["child-1"]["status"] == "active"


@pytest.mark.parametrize("participant_dropped", [False, True])
def test_persist_matches_resolve_participants_by_engine_id(
    isolated_db: str, participant_dropped: bool
) -> None:
    # Evolution can change matchup display text, so identity resolves by id
    # rather than text prefixes; dropped participants leave no dangling match.
    state = _final_state_with_features()
    matchup = state["tournament_matchups"][0]
    matchup["hypothesis_a"] = "drifted text A"
    matchup["hypothesis_b"] = "drifted text B"
    if participant_dropped:
        matchup["hypothesis_b_id"] = matchup["winner_id"] = "eng-hyp-gone"
    run = seed_run("CSC goal")
    _persist(run_id=run.id, final_state=state, db_path=isolated_db)

    matches = records.list_matches(run.id, db_path=isolated_db)
    if participant_dropped:
        assert matches == []
    else:
        [match] = matches
        assert (match["winner_id"], match["loser_id"]) == (
            "eng-hyp-a",
            "eng-hyp-b",
        )


def test_resumed_finalize_does_not_double_publish(isolated_db: str) -> None:
    run = seed_run("CSC goal")
    drained = _persist(
        run_id=run.id,
        final_state=_final_state_with_features(),
        db_path=isolated_db,
    )

    def _finalize(resumed: bool) -> list[Any]:
        return _drain(
            report_finalize.finalize_report(
                run.id,
                report_build.ReportRequest(
                    research_goal=run.research_goal,
                    run_mode="standard",
                    provider="engine",
                    execution_time=1.0,
                    db_path=isolated_db,
                    **drained.report_inputs,
                ),
                emit_event,
                resumed=resumed,
            )
        )

    first = _finalize(resumed=False)
    second = _finalize(resumed=True)

    assert any(e["type"] == "report" for e in first)
    assert second == []
    with db.connect(isolated_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM reports WHERE run_id=?", (run.id,)).fetchone()[0]
    assert count == 1


def _citations_citation_map() -> dict[str, Any]:
    return {
        "C1": {
            "type": "paper",
            "title": "CXCR1 drives CSC renewal",
            "url": "https://example.org/c1",
            "authors": ["Smith"],
            "year": 2023,
        },
        "C2": {
            "type": "paper",
            "title": "Unrelated off-target study",
            "url": "https://example.org/c2",
            "authors": ["Doe"],
            "year": 2021,
        },
        "C3": {
            "type": "knowledge_graph",
            "display": "STRING: CXCR1 -> STAT3",
        },
    }


def _final_state_with_citations() -> dict[str, Any]:
    grounding = "CXCR1 signaling drives breast cancer stem cell renewal"
    return {
        "hypotheses": [
            _engine_hypothesis(
                "eng-hyp-a",
                "Blocking CXCR1 suppresses breast cancer stem cells.",
                literature_grounding=grounding,
                citation_map=_citations_citation_map(),
                # A deep-verified finalist is featured with its references.
                deep_verification_verdict="holds",
            )
        ],
        "articles": [
            {
                "title": "CXCR1 drives CSC renewal",
                "url": "https://example.org/c1",
                "abstract": "CXCR1 signaling drives breast cancer stem "
                "cell renewal across xenograft models.",
                "authors": ["Smith"],
                "year": 2023,
            }
        ],
        "tournament_matchups": [],
        "meta_review": {},
        "research_overview": {},
    }


def test_persist_classifies_citations_via_shared_classifier(
    isolated_db: str,
) -> None:
    run = seed_run("CSC goal")
    _persist(
        run_id=run.id,
        final_state=_final_state_with_citations(),
        db_path=isolated_db,
    )

    citations = records.list_citations(run.id, db_path=isolated_db)
    states = {c["claim"]: c["state"] for c in citations}
    assert states == {
        "[C1] cited in hypothesis": "verified",
        "[C2] cited in hypothesis": "unsupported",
        "[C3] cited in hypothesis": "unavailable",
    }
    # Non-paper citations use display rather than title; falling back to the
    # citation key loses source identity.
    evidence = records.list_evidence(run.id, db_path=isolated_db)
    kg_row = next(e for e in evidence if e["source"] == "knowledge_graph")
    assert kg_row["title"] == "STRING: CXCR1 -> STAT3"

    # The rendered report resolves the grounding text's citation keys.
    _payload, markdown = asyncio.run(_build_report(run, isolated_db))
    section = markdown.split("#### References", 1)[1]
    assert "CXCR1 drives CSC renewal" in section
    assert "STRING: CXCR1 -> STAT3" in section
    assert "cited in hypothesis" not in section


@pytest.mark.parametrize(
    ("is_retracted", "correction_status"), [(True, "current"), (False, "retracted")]
)
def test_retraction_metadata_survives_reference_index_and_citation_persistence(
    isolated_db: str, is_retracted: bool, correction_status: str
) -> None:
    article = Article(
        title="CXCR1 drives CSC renewal",
        url="https://example.org/c1",
        abstract="CXCR1 signaling drives breast cancer stem cell renewal",
        used_in_analysis=True,
        is_retracted=is_retracted,
        correction_status=correction_status,
    )
    references = build_reference_index([article], None)
    citation_map = resolve_citation_keys("CXCR1 drives CSC renewal [C1]", references.sources)
    assert citation_map["C1"]["is_retracted"] is is_retracted
    assert citation_map["C1"]["correction_status"] == correction_status
    state = _final_state_with_citations()
    state["hypotheses"][0]["citation_map"] = citation_map
    run = seed_run("CSC goal")
    _persist(run_id=run.id, final_state=state, db_path=isolated_db)
    citations = records.list_citations(run.id, db_path=isolated_db)
    assert len(citations) == 1
    assert citations[0]["state"] == "unavailable"
