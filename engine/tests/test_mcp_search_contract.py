from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.tools import ToolException

from co_scientist.platform.retrieval.evidence.search_query import call_search_tool
from co_scientist.science.generation.literature_tools import validate


async def test_failed_search_is_permanent_and_never_reads_as_zero_prior_art() -> None:
    client = AsyncMock()
    client.call_tool.return_value = {"status": "failed", "records": [], "error": "network_error"}
    with pytest.raises(ToolException, match="network_error"):
        await call_search_tool(client, "search_pubmed", {"query": "weak claim"})
    assert client.call_tool.await_count == 1


async def test_down_backend_does_not_validate_a_weak_draft_as_novel() -> None:
    client = AsyncMock()
    client.call_tool.return_value = {"status": "failed", "records": [], "error": "network_error"}
    analyzer = AsyncMock()
    ctx = validate._NoveltySearchContext(client, None, "test", None)
    result = await validate._gather_hypothesis_novelty_analyses(
        1, 1, {"hypothesis": "Vitamin C cures every cancer"}, "offline/test", ctx, analyzer
    )
    assert result["search_status"] == "unknown"
    assert result["failed_sources"] == ["pubmed_search_with_fulltext"]
    assert result["novelty_analyses"] == []
    analyzer.assert_not_awaited()
    assert client.call_tool.await_count == 1


async def test_synthesis_cannot_award_novelty_credit_after_a_source_outage() -> None:
    batch: list[dict[str, Any]] = [
        {
            "draft": {"hypothesis": "Vitamin C cures every cancer"},
            "novelty_analyses": [],
            "search_status": "unknown",
            "failed_sources": ["search_pubmed"],
        }
    ]
    synthesis = AsyncMock(
        return_value=[
            {
                "hypothesis": "Vitamin C cures every cancer",
                "novelty_validation": {"decision": "approved", "novelty_score": 1.0},
            }
        ]
    )
    result = await validate._run_and_retry_synthesis_batches([batch], synthesis)
    assert result[0]["novelty_validation"]["decision"] == "unknown"
    assert result[0]["novelty_validation"]["novelty_score"] == 0
    assert result[0]["novelty_validation"]["failed_sources"] == ["search_pubmed"]


def test_an_outage_cannot_become_a_persisted_novelty_score() -> None:
    from co_scientist.domains.research_state.drain.hypotheses import _mean_review_novelty

    hypothesis = {
        "novelty_validation": {"decision": "unknown", "failed_sources": ["search_pubmed"]},
        "reviews": [{"scores": {"novelty": 9.0}}],
    }
    assert _mean_review_novelty(hypothesis) is None


def test_report_discloses_failed_sources_as_unknown_novelty() -> None:
    import json

    from co_scientist.domains.report.markdown import ReportMarkdownInputs, render_report_markdown

    report = render_report_markdown(
        ReportMarkdownInputs(
            research_goal="test",
            provider="offline",
            top_hypotheses=[],
            reviews=[
                {
                    "reviewer_agent": "novelty_validation",
                    "detail_json": json.dumps(
                        {
                            "decision": "unknown",
                            "failed_sources": ["search_pubmed", "search_openalex"],
                        }
                    ),
                }
            ],
        )
    )
    assert "search_pubmed" in report
    assert "search_openalex" in report
    assert "unknown" in report.lower()
    assert "no novelty credit" in report.lower()


async def test_failed_retrieval_does_not_remove_credit_from_an_independent_healthy_batch() -> None:
    failed = [{"search_status": "unknown", "failed_sources": ["search_pubmed"]}]
    healthy = [{"search_status": "ok"}]
    synthesis = AsyncMock(
        side_effect=[
            [{"hypothesis": "weak", "novelty_validation": {"decision": "approved"}}],
            [{"hypothesis": "supported", "novelty_validation": {"decision": "approved"}}],
        ]
    )
    result = await validate._run_and_retry_synthesis_batches([failed, healthy], synthesis)
    assert result[0]["novelty_validation"]["decision"] == "unknown"
    assert result[1]["novelty_validation"]["decision"] == "approved"


def test_failed_novelty_search_removes_review_axis_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from unittest.mock import Mock

    from co_scientist.domains.research_state.drain import reviews
    from co_scientist.domains.research_state.repository import records

    save = Mock()
    monkeypatch.setattr(records, "add_review", save)
    hypothesis = {
        "novelty_validation": {"decision": "unknown", "failed_sources": ["search_pubmed"]},
        "reviews": [{"scores": {"novelty": 9, "testability": 7}, "novel_aspects": ["No papers!"]}],
    }
    reviews._persist_engine_review_rows("run", "hyp", hypothesis, Mock())
    row = save.call_args.args[0]
    assert row.novelty is None
    detail = json.loads(row.detail_json)
    assert "novelty" not in detail["scores"]
    assert detail["scores"]["testability"] == 7
    assert "No papers!" not in row.critique


def test_an_outage_clears_an_earlier_stored_novelty_score(monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    from unittest.mock import Mock

    from co_scientist.domains.research_state.drain import hypotheses
    from co_scientist.domains.research_state.repository import hypotheses as repository

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE hypothesis_state (hypothesis_id TEXT, elo_rating INTEGER, "
        "win_count INTEGER, loss_count INTEGER, novelty_score REAL, status TEXT, "
        "verification_verdict TEXT, updated_at REAL)"
    )
    conn.execute("INSERT INTO hypothesis_state VALUES ('hyp', 1200, 0, 0, 9, 'reviewed', NULL, 0)")
    monkeypatch.setattr(repository, "add_hypothesis", Mock(return_value="hyp"))
    hypotheses._persist_engine_hypothesis_row(
        "run",
        {
            "id": "hyp",
            "text": "Vitamin C cures every cancer",
            "novelty_validation": {"decision": "unknown", "failed_sources": ["search_pubmed"]},
            "reviews": [{"scores": {"novelty": 9}}],
        },
        set(),
        conn,
    )
    assert conn.execute("SELECT novelty_score FROM hypothesis_state").fetchone()[0] is None
    conn.close()


def test_report_lists_failed_research_calls_beside_novelty_failures() -> None:
    from co_scientist.domains.report.markdown import ReportMarkdownInputs, render_report_markdown

    report = render_report_markdown(
        ReportMarkdownInputs(
            research_goal="test",
            provider="offline",
            top_hypotheses=[],
            retrieval_calls=[
                {"source": "search_arxiv", "status": "failed", "error": "network_error"},
                {"source": "search_openalex", "status": "empty"},
            ],
        )
    )
    assert "failed sources: search_arxiv" in report
    assert "failed sources: search_openalex" not in report
