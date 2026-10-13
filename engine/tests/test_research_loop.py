from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from co_scientist.platform.retrieval.research import (
    CallStatus,
    SourceHit,
    StopReason,
    ThreadStatus,
    conduct_research,
)
from tests._research_fakes import FakeModel, FakeRetrieval, _budget, _hits


async def test_all_failed_searches_fail_the_thread_but_keep_the_no_results_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = await conduct_research(
        goal="fibrosis",
        model=FakeModel(),
        retrieval=FakeRetrieval(failing_sources={"pubmed", "corpus"}),
        budget=_budget(depth=1, sources=("pubmed", "corpus")),
        seed_questions=["q1"],
    )

    assert result.stop_reason is StopReason.NO_RESULTS
    assert {call.status for call in result.calls} == {CallStatus.FAILED}
    assert [thread.status for thread in result.threads] == [ThreadStatus.FAILED]
    assert "1 failed, 0 empty" in caplog.text


async def test_successful_searches_with_no_hits_remain_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = await conduct_research(
        goal="fibrosis",
        model=FakeModel(),
        retrieval=FakeRetrieval(),
        budget=_budget(depth=1),
        seed_questions=["q1"],
    )

    assert [call.status for call in result.calls] == [CallStatus.EMPTY]
    assert [thread.status for thread in result.threads] == [ThreadStatus.EMPTY]
    assert "0 failed, 1 empty" in caplog.text


async def test_failed_reads_without_snippets_fail_without_extraction() -> None:
    class UnreadableRetrieval(FakeRetrieval):
        async def read(self, *, locator: str) -> str | None:
            raise RuntimeError(f"{locator} is unreachable")

    hit = SourceHit(locator="doc-a", title="title doc-a", snippet="", rank=0)
    model = FakeModel(barren={"q1"})
    result = await conduct_research(
        goal="fibrosis",
        model=model,
        retrieval=UnreadableRetrieval({"pubmed": [hit]}),
        budget=_budget(depth=1),
        seed_questions=["q1"],
    )

    assert result.threads[0].status is ThreadStatus.FAILED
    assert not result.findings
    assert model.extracted == []


async def test_snippet_fallback_can_still_produce_a_finding() -> None:
    result = await conduct_research(
        goal="fibrosis",
        model=FakeModel(),
        retrieval=FakeRetrieval({"pubmed": _hits("doc-a")}, unreadable={"doc-a"}),
        budget=_budget(depth=1),
        seed_questions=["q1"],
    )

    assert result.threads[0].status is ThreadStatus.OK
    assert result.findings
    assert result.threads[0].finding_ids
    assert "snippets were used" in (result.threads[0].note or "")


async def test_snippet_fallback_with_no_findings_remains_empty() -> None:
    result = await conduct_research(
        goal="fibrosis",
        model=FakeModel(barren={"q1"}),
        retrieval=FakeRetrieval({"pubmed": _hits("doc-a")}, unreadable={"doc-a"}),
        budget=_budget(depth=1),
        seed_questions=["q1"],
    )

    assert result.threads[0].status is ThreadStatus.EMPTY
    assert not result.findings
    assert "snippets were used" in (result.threads[0].note or "")


async def test_partial_document_read_success_keeps_no_findings_as_empty() -> None:
    result = await conduct_research(
        goal="fibrosis",
        model=FakeModel(barren={"q1"}),
        retrieval=FakeRetrieval({"pubmed": _hits("doc-a", "doc-b")}, unreadable={"doc-b"}),
        budget=_budget(depth=1),
        seed_questions=["q1"],
    )

    assert result.threads[0].status is ThreadStatus.EMPTY
    assert not result.findings
    assert "Some full-text reads failed" in (result.threads[0].note or "")


async def test_results_the_budget_refused_are_recorded_as_refused() -> None:
    """Replay needs the record of seen-but-unread results, not only accepted
    evidence."""
    model = FakeModel()
    retrieval = FakeRetrieval(
        {
            "pubmed": _hits("doc-a", "doc-b"),
            "corpus": _hits("doc-c", "doc-d"),
        }
    )

    result = await conduct_research(
        goal="fibrosis",
        model=model,
        retrieval=retrieval,
        budget=_budget(
            depth=1,
            breadth=1,
            hits_per_question=2,
            sources=("pubmed", "corpus"),
        ),
        seed_questions=["q1"],
    )

    by_source = {call.source: call for call in result.calls}
    assert by_source["pubmed"].admitted == ("doc-a", "doc-b")
    assert by_source["pubmed"].dropped == ()
    assert by_source["corpus"].admitted == ()
    assert by_source["corpus"].dropped == ("doc-c", "doc-d")


async def test_the_loop_never_opens_more_threads_than_it_may() -> None:
    model = FakeModel(
        stances=("mechanism", "prior art", "contradictions", "methods"),
        follow_ups_by_question={},
    )
    model.follow_ups_by_question = _Everything(["f1", "f2", "f3", "f4", "f5", "f6"])
    retrieval = FakeRetrieval({"pubmed": _hits("doc-a")})
    budget = _budget(depth=3, breadth=4)

    result = await conduct_research(
        goal="fibrosis", model=model, retrieval=retrieval, budget=budget
    )

    started = [t for t in result.threads if t.status is not ThreadStatus.DECLINED]
    assert len(started) <= budget.max_threads()
    assert result.levels_run == 3
    assert len(started) == 8


class _Everything(dict):  # type: ignore[type-arg]
    """Fresh questions avoid exercising repeated-question termination instead
    of depth bounds."""

    def __init__(self, value: list[str]) -> None:
        super().__init__()
        self._value = value
        self._asked = 0

    def get(self, key: object, default: object = None) -> list[str]:
        self._asked += 1
        return [f"{text}-{self._asked}" for text in self._value]


def test_the_package_depends_on_nothing_in_this_repo_but_itself() -> None:
    """Adapters cease to be interchangeable if the capability imports its
    caller."""
    package = pathlib.Path(conduct_research.__module__.replace(".", "/"))
    root = pathlib.Path(__file__).parents[1] / "src" / package.parent
    borrowed: set[str] = set()
    for module in sorted(root.glob("*.py")):
        tree = ast.parse(module.read_text(), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                borrowed.add(node.module)
            elif isinstance(node, ast.Import):
                borrowed.update(alias.name for alias in node.names)

    outside = {
        name
        for name in borrowed
        if name.startswith("co_scientist")
        and not name.startswith("co_scientist.platform.retrieval.research")
    }
    assert not outside


def test_no_production_module_reads_the_raw_criteria_keys() -> None:
    src_root = Path(__file__).resolve().parents[1] / "src" / "co_scientist"
    pattern = re.compile(r"go_criterion|no_go_criterion")
    offenders = []
    for path in src_root.rglob("*.py"):
        rel = path.relative_to(src_root).as_posix()
        if rel in _ALLOWED_READERS:
            continue
        source = path.read_text(encoding="utf-8")
        if rel == "science/citations.py":
            formatter = next(
                node
                for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "format_experiment_plan"
            )
            lines = source.splitlines()
            del lines[formatter.lineno - 1 : formatter.end_lineno]
            source = "\n".join(lines)
        if pattern.search(source):
            offenders.append(rel)
    assert offenders == []


# This list covers raw experiment readers outside schema and formatting
# boundaries.
_ALLOWED_READERS = {
    "science/schemas/generation.py",
}


async def test_questions_with_one_query_share_a_search_and_sources_fan_out_in_bounded_number() -> (
    None
):
    from co_scientist.platform.retrieval.research.loop import SOURCE_FANOUT

    sources = tuple(f"source-{index}" for index in range(SOURCE_FANOUT + 2))
    in_flight = 0
    peak = 0

    class SlowRetrieval(FakeRetrieval):
        async def search(self, *, query: str, source: str, limit: int) -> Sequence[SourceHit]:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return await super().search(query=query, source=source, limit=limit)

    class OneQueryModel(FakeModel):
        async def to_query(self, *, question: str) -> str:
            return "fibrosis TGF-beta"

    retrieval = SlowRetrieval({source: _hits(f"doc-{source}") for source in sources})

    result = await conduct_research(
        goal="fibrosis",
        model=OneQueryModel(),
        retrieval=retrieval,
        budget=_budget(depth=1, breadth=2, concurrency=2, sources=sources),
        seed_questions=["q1", "q2"],
    )

    assert len(retrieval.queries) == len(sources)
    assert peak <= SOURCE_FANOUT
    # Each thread still records its own calls as provenance.
    assert len(result.calls) == 2 * len(sources)
    assert all(thread.status is ThreadStatus.OK for thread in result.threads)
