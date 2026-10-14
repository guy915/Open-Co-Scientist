from __future__ import annotations

import asyncio
from typing import Any

import pytest
from co_scientist.domains.report import gates as report_gates
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state import models as engine_models
from co_scientist.domains.research_state.repository import hypotheses, records
from co_scientist.domains.research_state.repository.hypotheses import NewHypothesis
from co_scientist.domains.research_state.repository.records import NewClaimEvidence, NewReview
from co_scientist.orchestration.engine_tasks import inputs as engine_tasks_inputs
from co_scientist.orchestration.repository import runs_views as views
from co_scientist.platform.db import retrieval_calls as retrieval
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import RunStatus
from co_scientist.platform.retrieval.research import (
    CallStatus,
    Finding,
    Question,
    ResearchResult,
    SearchCall,
    SourceHit,
    StopReason,
    ThreadRecord,
    ThreadStatus,
    result_to_dict,
)

from tests._drain_helpers import (
    _build_report,
    _final_state_with_features,
    _final_state_with_lineage,
    _persist,
    _persist_and_finalize,
)
from tests._store_helpers import seed_run

_CALL = SearchCall(
    question="What blocks TGF-beta signalling in humans?",
    query="TGF-beta blockade human fibrosis",
    source="pubmed",
    status=CallStatus.OK,
    hits=(
        SourceHit(
            locator="12345678",
            title="A researched paper",
            snippet="TGF-beta blockade reduced fibrosis.",
            rank=0,
            score=0.8,
        ),
        SourceHit(locator="99999999", title="Also seen", snippet="", rank=1),
    ),
    admitted=("12345678",),
    dropped=("99999999",),
    duration_seconds=0.6,
)


def _ledger() -> dict[str, Any]:
    finding = Finding(
        text="TGF-beta blockade reduced fibrosis in a human cohort",
        question=_CALL.question,
        locator="12345678",
        span="TGF-beta blockade reduced fibrosis.",
        call_id=_CALL.id,
    )
    ledger: dict[str, Any] = result_to_dict(
        ResearchResult(
            goal="reverse fibrosis",
            stances=(),
            threads=(
                ThreadRecord(
                    question=Question(text=_CALL.question, stance="seed"),
                    depth=1,
                    status=ThreadStatus.OK,
                    call_ids=(_CALL.id,),
                    finding_ids=(finding.id,),
                    summary="Human evidence exists but is thin.",
                ),
            ),
            calls=(_CALL,),
            findings=(finding,),
            stop_reason=StopReason.NO_FOLLOW_UPS,
            levels_run=1,
        )
    )
    return ledger


def _provenance_final_state(*, researched: bool) -> dict[str, Any]:
    article: dict[str, Any] = {
        "title": "A researched paper",
        "source": "pubmed",
        "source_id": "12345678",
        "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
        "abstract": "TGF-beta blockade reduced fibrosis.",
    }
    state: dict[str, Any] = {
        "hypotheses": [],
        "articles": [article],
        "tournament_matchups": [],
        "meta_review": {},
        "evolution_details": [],
        "research_overview": {},
    }
    if researched:
        article["retrieval_call_id"] = _CALL.id
        state["research_ledgers"] = [_ledger()]
    return state


def test_evidence_resolves_to_the_search_that_found_it(
    isolated_db: str,
) -> None:
    # Search ledger and evidence search link must persist together; either half
    # alone loses usable provenance.
    run = seed_run("provenance goal", profile="extended")

    _persist_and_finalize(run, _provenance_final_state(researched=True), isolated_db)

    evidence = records.list_evidence(run.id, db_path=isolated_db)
    calls = retrieval.list_retrieval_calls(run.id, db_path=isolated_db)
    assert len(evidence) == 1
    assert len(calls) == 1
    by_id = {call["id"]: call for call in calls}
    found_by = by_id[evidence[0]["retrieval_call_id"]]
    assert found_by["query"] == "TGF-beta blockade human fibrosis"
    assert found_by["question"] == _CALL.question
    assert found_by["source"] == "pubmed"
    assert [hit["locator"] for hit in found_by["hits"]] == [
        "12345678",
        "99999999",
    ]
    assert found_by["admitted"] == ["12345678"]
    assert found_by["dropped"] == ["99999999"]
    assert found_by["depth"] == 1
    _, markdown = asyncio.run(_build_report(run, isolated_db))
    assert "## Data sources" in markdown
    assert _CALL.question in markdown


def test_a_run_that_did_no_research_writes_no_searches(
    isolated_db: str,
) -> None:
    run = seed_run("shallow goal")

    _persist_and_finalize(run, _provenance_final_state(researched=False), isolated_db)

    assert retrieval.list_retrieval_calls(run.id, db_path=isolated_db) == []
    evidence = records.list_evidence(run.id, db_path=isolated_db)
    assert evidence[0]["retrieval_call_id"] is None


_REVIEW_CALL = SearchCall(
    question="Is the receptor expressed in humans?",
    query="TGF-beta receptor human expression",
    source="pubmed",
    status=CallStatus.OK,
    hits=(
        SourceHit(
            locator="55555555",
            title="Expression atlas",
            snippet="The receptor is expressed in human lung.",
            rank=0,
        ),
    ),
    admitted=("55555555",),
)


def _review_ledger() -> dict[str, Any]:
    ledger: dict[str, Any] = result_to_dict(
        ResearchResult(
            goal="reverse fibrosis",
            stances=(),
            threads=(
                ThreadRecord(
                    question=Question(text=_REVIEW_CALL.question, stance="seed"),
                    depth=1,
                    status=ThreadStatus.OK,
                    call_ids=(_REVIEW_CALL.id,),
                ),
            ),
            calls=(_REVIEW_CALL,),
            findings=(),
            stop_reason=StopReason.NO_FOLLOW_UPS,
            levels_run=1,
        )
    )
    return ledger


def test_every_researcher_keeps_its_own_searches_and_papers(
    isolated_db: str,
) -> None:
    # Literature and reflection own separate ledgers; one shared writer silently
    # replaces earlier provenance, and a repeated search is still one row.
    run = seed_run("two researchers", profile="ultra")
    state = _provenance_final_state(researched=True)
    state["research_ledgers"] += [_review_ledger(), _ledger()]
    state["articles"].append(
        {
            "title": "Expression atlas",
            "source": "pubmed",
            "source_id": "55555555",
            "url": "https://pubmed.ncbi.nlm.nih.gov/55555555/",
            "abstract": "The receptor is expressed in human lung.",
            "retrieval_call_id": _REVIEW_CALL.id,
        }
    )

    _persist_and_finalize(run, state, isolated_db)

    calls = {
        call["id"]: call for call in retrieval.list_retrieval_calls(run.id, db_path=isolated_db)
    }
    assert {call["query"] for call in calls.values()} == {
        "TGF-beta blockade human fibrosis",
        "TGF-beta receptor human expression",
    }
    by_title = {row["title"]: row for row in records.list_evidence(run.id, db_path=isolated_db)}
    found_by = calls[by_title["Expression atlas"]["retrieval_call_id"]]
    assert found_by["question"] == "Is the receptor expressed in humans?"


@pytest.mark.parametrize(
    "degradation",
    [
        {
            "reason": "mcp_unreachable",
            "lost": ["literature_review", "deep_research"],
            "floor": "none",
        },
        None,
    ],
)
def test_the_report_carries_what_the_run_could_not_search(
    isolated_db: str, degradation: dict[str, Any] | None
) -> None:
    # No-source degradation is invisible from otherwise complete ideas and
    # tournaments; reports must disclose it.
    run = seed_run("degraded goal", profile="extended")

    _persist_and_finalize(
        run,
        {**_final_state_with_features(), "retrieval_degradation": degradation},
        isolated_db,
    )

    report = reports.get_latest_report(run.id, db_path=isolated_db)
    assert report is not None
    assert report["payload"]["retrieval_degradation"] == degradation


def _seed_labelled_hypothesis(
    run: Any, title: str, statement: str, label: str | None, db_path: str
) -> str:
    hypothesis_id = hypotheses.add_hypothesis(
        NewHypothesis(run_id=run.id, title=title, statement=statement),
        db_path=db_path,
    )
    if label is not None:
        refuting = label == "contradicts"
        records.add_claim_evidence(
            NewClaimEvidence(
                run_id=run.id,
                hypothesis_id=hypothesis_id,
                claim=statement,
                label=label,
                supporting=[] if refuting else ["A source span."],
                contradicting=["A refuting span."] if refuting else [],
                assessor="fixture",
            ),
            db_path=db_path,
        )
    return hypothesis_id


def test_rank_and_publish_splits_contradicted_from_unverified(
    isolated_db: str,
) -> None:
    run = seed_run("gate split")
    ids = {
        label: _seed_labelled_hypothesis(
            run, label, f"Statement about {label}.", label, isolated_db
        )
        for label in ("supports", "contradicts", "insufficient", "partial")
    }
    ids["none"] = _seed_labelled_hypothesis(run, "none", "Statement about none.", None, isolated_db)

    contradicted = report_gates.contradicted_hypothesis_ids(run.id, isolated_db)
    unverified = report_gates.unverified_hypothesis_ids(run.id, isolated_db)
    assert contradicted == {ids["contradicts"]}
    # Partial support is relevant and consistent, so it clears the badge.
    assert unverified == {ids["none"], ids["contradicts"], ids["insufficient"]}

    hyps = hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    kept_ids = {h["id"] for h in report_gates.exclude_unsafe_hypotheses(run.id, hyps, isolated_db)}
    assert kept_ids == set(ids.values()) - {ids["contradicts"]}

    _assert_demo_run_badges_nothing(isolated_db)


def _assert_demo_run_badges_nothing(db_path: str) -> None:
    demo = seed_run("demo", provider="mock")
    hypotheses.add_hypothesis(
        NewHypothesis(run_id=demo.id, title="Demo", statement="Demo idea."),
        db_path=db_path,
    )
    assert report_gates.unverified_hypothesis_ids(demo.id, db_path) == set()


def _drained_status(disposition: str | None, isolated_db: str, goal: str) -> str:
    state = _final_state_with_lineage()
    state["hypotheses"][0]["review_disposition"] = disposition
    run = seed_run(goal)
    _persist(run_id=run.id, final_state=state, db_path=isolated_db)
    by_id = {
        hypothesis["id"]: hypothesis
        for hypothesis in hypotheses.list_hypotheses(run.id, db_path=isolated_db)
    }
    return str(by_id["parent-1"]["status"])


@pytest.mark.parametrize(
    ("disposition", "status"),
    [
        *((d, "rejected") for d in sorted(engine_models.BLOCKING_REVIEW_DISPOSITIONS)),
        (None, "active"),
        ("needs_revision", "active"),
    ],
)
def test_engine_review_dispositions_decide_what_the_drain_publishes(
    isolated_db: str, disposition: str | None, status: str
) -> None:
    assert _drained_status(disposition, isolated_db, f"{disposition} goal") == status


def test_offline_run_with_empty_leaderboard_is_blocked_like_a_real_run(
    isolated_db: str,
) -> None:
    # Offline scientific readiness is the same gate; only curated demos write
    # reports outside finalization.
    state = _final_state_with_lineage()
    for hypothesis in state["hypotheses"]:
        hypothesis["review_disposition"] = "unsafe"
    run = seed_run(
        "offline empty leaderboard goal",
        llm_backend="offline",
        db_path=isolated_db,
    )

    _persist_and_finalize(run, state, isolated_db)

    settled = runs.get_run(run.id, db_path=isolated_db)
    assert settled is not None
    assert settled.status == RunStatus.BLOCKED.value
    assert reports.get_latest_report(run.id, db_path=isolated_db) is None
    assert settled.error is not None
    assert "peer review" in settled.error
    assert "safety review" not in settled.error
    assert "contradicted" not in settled.error


def _seed_scientist_hypothesis(run_id: str, db_path: str) -> str:
    return hypotheses.add_hypothesis(
        NewHypothesis(
            run_id=run_id,
            title="Scientist idea",
            statement="A scientist-proposed mechanism for kinase X.",
            created_by_agent="scientist_manual",
            author="dr-who",
        ),
        db_path=db_path,
    )


def _seed_scientist_review(run_id: str, hypothesis_id: str, db_path: str) -> None:
    records.add_review(
        NewReview(
            run_id=run_id,
            hypothesis_id=hypothesis_id,
            reviewer_agent="scientist",
            summary="Scientist verdict: oppose (by dr-who)",
            critique="The proposed control cannot distinguish the mechanism.",
            author="dr-who",
            verdict="oppose",
        ),
        db_path=db_path,
    )


def _merged_final_state(run_id: str, db_path: str) -> dict[str, Any]:
    state: dict[str, Any] = {"hypotheses": []}
    engine_tasks_inputs._merge_scientist_inputs(state, run_id, db_path)
    return {
        "hypotheses": [h.to_dict() for h in state["hypotheses"]],
        "articles": [],
        "tournament_matchups": [],
        "proximity_graph": {},
        "meta_review": {},
        "research_overview": {},
    }


def _replay_finalize(run_id: str, final_state: dict[str, Any], db_path: str) -> None:
    views.clear_publication_artifacts(run_id, db_path=db_path)
    _persist(run_id=run_id, final_state=final_state, db_path=db_path)


def test_drained_scientist_hypothesis_and_review_keep_their_rows(
    isolated_db: str,
) -> None:
    # Scientist rows survive publication resets; reconcile them instead of
    # inserting colliding copies.
    from co_scientist.core.constants import NOT_VIABLE_SCORE

    run = seed_run("Scientist drain", profile="express")
    hypothesis_id = _seed_scientist_hypothesis(run.id, isolated_db)
    _seed_scientist_review(run.id, hypothesis_id, isolated_db)
    final_state = _merged_final_state(run.id, isolated_db)
    final_state["hypotheses"][0]["win_count"] = 3
    final_state["hypotheses"][0]["loss_count"] = 1
    # Human verdicts share agent review scales; off-scale scores mislead
    # tournament judges.
    merged_review = final_state["hypotheses"][0]["reviews"][0]
    assert merged_review["overall_score"] == NOT_VIABLE_SCORE

    _replay_finalize(run.id, final_state, isolated_db)
    _replay_finalize(run.id, final_state, isolated_db)

    rows = hypotheses.list_hypotheses(run.id, isolated_db)
    assert [row["id"] for row in rows] == [hypothesis_id]
    assert (rows[0]["win_count"], rows[0]["loss_count"]) == (3, 1)
    assert rows[0]["author"] == "dr-who"
    assert rows[0]["created_by_agent"] == "scientist_manual"
    assert rows[0]["parent_id"] is None
    reviews = records.list_reviews(run.id, isolated_db)
    assert [row["reviewer_agent"] for row in reviews] == ["scientist"]
    assert reviews[0]["author"] == "dr-who"
    assert reviews[0]["verdict"] == "oppose"
