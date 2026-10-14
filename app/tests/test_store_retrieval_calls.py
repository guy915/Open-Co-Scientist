# Retrieval ids omit run identity; persisted keys must include it even for empty
# or failed searches.

from __future__ import annotations

import os

import pytest
from co_scientist.domains.research_state.drain.matches import retrieval_call_rows
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.records import NewEvidence
from co_scientist.platform.db import retrieval_calls as store
from co_scientist.platform.db import runs
from co_scientist.platform.retrieval.research import (
    CallStatus,
    Question,
    ResearchResult,
    SearchCall,
    SourceHit,
    StopReason,
    ThreadRecord,
    ThreadStatus,
)

from tests._store_helpers import seed_run


@pytest.fixture
def db(isolated_db: str) -> str:
    return os.environ["COSCIENTIST_DB_PATH"]


def _hit(locator: str, rank: int = 0) -> SourceHit:
    return SourceHit(
        locator=locator,
        title=f"Title {locator}",
        snippet=f"Snippet for {locator}",
        rank=rank,
        score=1.0 - rank / 10,
        metadata={"kind": "article"},
    )


def _result(
    *,
    question: str = "What drives fibrosis?",
    depth: int = 1,
    calls: list[SearchCall] | None = None,
) -> ResearchResult:
    asked = Question(text=question, stance="mechanism")
    made = calls or [
        SearchCall(
            question=question,
            query="fibrosis mechanism",
            source="pubmed",
            status=CallStatus.OK,
            hits=(_hit("doc-a"), _hit("doc-b", rank=1)),
            admitted=("doc-a",),
            dropped=("doc-b",),
            duration_seconds=0.4,
        )
    ]
    return ResearchResult(
        goal="fibrosis",
        stances=("mechanism",),
        threads=(
            ThreadRecord(
                question=asked,
                depth=depth,
                status=ThreadStatus.OK,
                call_ids=tuple(call.id for call in made),
            ),
        ),
        calls=tuple(made),
        findings=(),
        stop_reason=StopReason.DEPTH_EXHAUSTED,
        levels_run=depth,
    )


def test_a_search_round_trips_with_its_question_and_ranking(db: str) -> None:
    run = seed_run("provenance")
    result = _result()

    store.add_retrieval_calls(retrieval_call_rows(run.id, result))

    rows = store.list_retrieval_calls(run.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["question"] == "What drives fibrosis?"
    assert row["query"] == "fibrosis mechanism"
    assert row["source"] == "pubmed"
    assert row["status"] == "ok"
    assert row["depth"] == 1
    assert [hit["locator"] for hit in row["hits"]] == ["doc-a", "doc-b"]
    assert [hit["rank"] for hit in row["hits"]] == [0, 1]
    assert row["admitted"] == ["doc-a"]
    assert row["dropped"] == ["doc-b"]
    assert row["question_id"] == result.threads[0].question.id


def test_the_same_search_persisted_twice_is_one_row(db: str) -> None:
    run = seed_run("resume")
    rows = retrieval_call_rows(run.id, _result())

    assert store.add_retrieval_calls(rows) == 1
    assert store.add_retrieval_calls(rows) == 0

    assert len(store.list_retrieval_calls(run.id)) == 1


def test_two_runs_asking_the_same_thing_keep_separate_rows(db: str) -> None:
    # Identical retrieval calls in different runs must not share or delete
    # provenance.
    first = seed_run("goal")
    second = seed_run("goal")
    result = _result()

    store.add_retrieval_calls(retrieval_call_rows(first.id, result))
    store.add_retrieval_calls(retrieval_call_rows(second.id, result))

    assert len(store.list_retrieval_calls(first.id)) == 1
    assert len(store.list_retrieval_calls(second.id)) == 1
    assert (
        store.list_retrieval_calls(first.id)[0]["id"]
        == store.list_retrieval_calls(second.id)[0]["id"]
    )

    runs.delete_run(first.id)
    assert len(store.list_retrieval_calls(second.id)) == 1


def test_evidence_can_name_the_search_that_found_it(db: str) -> None:
    run = seed_run("link")
    result = _result()
    rows = retrieval_call_rows(run.id, result)
    store.add_retrieval_calls(rows)

    ev_id = records.add_evidence(
        NewEvidence(
            run_id=run.id,
            title="Title doc-a",
            source="pubmed",
            retrieval_call_id=rows[0].id,
        )
    )

    stored = {row["id"]: row for row in records.list_evidence(run.id)}
    assert stored[ev_id]["retrieval_call_id"] == rows[0].id
