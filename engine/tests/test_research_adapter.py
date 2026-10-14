from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from co_scientist.platform.retrieval.research import (
    CallStatus,
    Finding,
    Question,
    ResearchResult,
    RetrievalError,
    SearchCall,
    SourceHit,
    StopReason,
    ThreadRecord,
    ThreadStatus,
    result_from_dict,
    result_to_dict,
)
from co_scientist.platform.retrieval.research_adapter import McpRetrieval, ResearchRun
from tests._research_fakes import (
    FakeResearchClient,
    research_registry,
    research_workflow,
)


def _result() -> ResearchResult:
    call = SearchCall(
        question="What drives fibrosis?",
        query="fibrosis mechanism",
        source="pubmed",
        status=CallStatus.OK,
        hits=(
            SourceHit(
                locator="doc-a",
                title="First",
                snippet="about a",
                rank=0,
                score=0.9,
                metadata={"doi": "10.1/a"},
            ),
            SourceHit(locator="doc-b", title="Second", snippet="about b", rank=1),
        ),
        admitted=("doc-a",),
        dropped=("doc-b",),
        duration_seconds=0.4,
    )
    finding = Finding(
        text="TGF-beta drives it",
        question="What drives fibrosis?",
        locator="doc-a",
        span="TGF-beta signalling drives fibrosis",
        call_id=call.id,
    )
    return ResearchResult(
        goal="reverse fibrosis",
        stances=("mechanism", "counter-evidence"),
        threads=(
            ThreadRecord(
                question=Question(text="What drives fibrosis?", stance="mechanism"),
                depth=1,
                status=ThreadStatus.OK,
                call_ids=(call.id,),
                finding_ids=(finding.id,),
                follow_ups=("what blocks it?",),
                summary="TGF-beta is the consensus driver.",
            ),
            ThreadRecord(
                question=Question(text="what blocks it?", stance="mechanism"),
                depth=2,
                status=ThreadStatus.DECLINED,
                note="breadth exhausted",
                retry_breadth=4,
            ),
        ),
        calls=(call,),
        findings=(finding,),
        stop_reason=StopReason.NO_FOLLOW_UPS,
        levels_run=2,
    )


def test_a_result_survives_the_trip_through_plain_data() -> None:
    original = _result()

    restored = result_from_dict(result_to_dict(original))

    assert restored == original


def _retrieval(tmp_path: Path, client: Any) -> McpRetrieval:
    registry = research_registry(tmp_path)
    workflow = research_workflow(registry)
    return McpRetrieval(
        client,
        registry,
        workflow,
        ResearchRun(run_id="run-1", research_goal="fibrosis reversal"),
    )


@pytest.mark.parametrize(
    ("source", "answer", "detail"),
    [
        ("gamma", None, ""),
        ("alpha", RuntimeError("connection refused"), "connection refused"),
    ],
    ids=["unconfigured", "broken"],
)
async def test_a_failing_source_is_a_retrieval_error_naming_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    answer: Exception | None,
    detail: str,
) -> None:
    monkeypatch.setattr(
        "co_scientist.platform.retrieval.evidence.search_query._search_retry_delay",
        lambda attempt: 0.0,
    )
    client = FakeResearchClient({"search_alpha": answer})
    retrieval = _retrieval(tmp_path, client)

    with pytest.raises(RetrievalError) as caught:
        await retrieval.search(query="q", source=source, limit=2)

    assert caught.value.source == source
    assert detail in str(caught.value)
