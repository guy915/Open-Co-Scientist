from __future__ import annotations

import json
from typing import Any

import pytest

from co_scientist.core.constants import (
    LITERATURE_REVIEW_FAILED,
)
from co_scientist.platform.llm.offline import llm as offline_llm
from co_scientist.platform.retrieval.evidence import (
    article_support,
    relevance,
    retrieval_support,
    search,
    search_support,
)
from co_scientist.science.generation.literature_review import (
    literature_review_node,
)
from tests._llm_fake import install_fake_llm
from tests._research_fakes import (
    _stub_node,
)
from tests._state import make_state


def test_published_retraction_metadata_is_explicit_even_outside_search() -> None:
    # Search excludes withdrawn records; attachments and probes also project
    # articles through this shared publishing boundary.
    articles = article_support.build_articles_from_metadata(
        {
            "flag": {"is_retracted": True, "correction_status": "retracted"},
            "type": {
                "publication_types": [
                    "Journal Article",
                    "Retracted Publication",
                ]
            },
        },
        "pubmed",
    )
    assert all(
        article.is_retracted and article.correction_status == "retracted" for article in articles
    )


def test_merge_search_results_ranks_quality_and_flags_retractions() -> None:
    """Retraction overrides source rank even when a source ranks it first."""
    source_results = [
        (
            "openalex",
            {
                "weak": {
                    "title": "Weak",
                    "source": "openalex",
                    "year": 2000,
                },
                "strong": {
                    "title": "Strong",
                    "source": "pubmed",
                    "year": 2026,
                    "cited_by_count": 1000,
                },
                "retracted": {
                    "title": "Retracted",
                    "source": "pubmed",
                    "is_retracted": True,
                },
            },
        )
    ]

    merged, _ = search_support.merge_search_results(source_results)

    assert list(merged) == ["weak", "strong", "retracted"]
    assert merged["retracted"]["correction_status"] == "retracted"


@pytest.fixture(autouse=True)
def _hermetic_node_model(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_llm(monkeypatch)


async def test_an_unreachable_server_fails_the_review_and_records_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A setup-time outage routes around this node; an outage here must mark
    report degradation."""
    fake_client = _stub_node(monkeypatch, server_available=False)
    state = make_state(research_goal="cancer immunotherapy resistance")
    state["context_enrichment_sources"] = [{"title": "an attachment"}]

    result = await literature_review_node(state)

    assert result["articles_with_reasoning"] == LITERATURE_REVIEW_FAILED
    assert result["literature_review_queries"] == []
    assert result["articles"] == []
    assert result["messages"][0]["metadata"]["error"] is True
    assert fake_client.calls == []
    degradation = result["retrieval_degradation"]
    assert degradation["reason"] == "mcp_unreachable"
    assert degradation["floor"] == "run_attachments"


async def test_abstract_only_papers_are_analyzed_with_bounded_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    papers = {
        "PMID5": {
            "title": "Abstract-only paper",
            "abstract": "Just an abstract, no body.",
        },
    }
    _stub_node(
        monkeypatch,
        server_available=True,
        search_payload=papers,
        queries=["q"],
    )
    state = make_state(research_goal="abstract only goal")

    result = await literature_review_node(state)

    assert result["articles_with_reasoning"] == "SYNTHESIZED REVIEW"
    assert result["literature_review_queries"] == ["q"]
    assert len(result["articles"]) == 1
    assert result["articles"][0].source_id == "PMID5"
    assert result["articles"][0].content is None
    assert result["articles"][0].abstract == "Just an abstract, no body."
    assert result["articles"][0].used_in_analysis is True
    assert result["messages"][0]["metadata"]["articles_analyzed"] == 1


def _candidate(title: str, lexical_score: float) -> dict[str, object]:
    return {
        "title": title,
        "abstract": f"Abstract for {title}.",
        "retrieval_score": lexical_score,
    }


async def _ranked_review(
    monkeypatch: pytest.MonkeyPatch,
    pool: dict[str, Any],
    *,
    goal: str = "goal",
    count: int | None = None,
) -> dict[str, Any]:
    _stub_node(monkeypatch, server_available=True, search_payload=pool)
    monkeypatch.setattr(search, "merge_search_results", lambda *args, **kwargs: (pool, {}))
    result = await literature_review_node(
        make_state(
            research_goal=goal,
            model_name=offline_llm.DEFAULT_OFFLINE_MODEL,
            literature_review_papers_count=len(pool) if count is None else count,
        )
    )
    return {article.source_id: article for article in result["articles"]}


async def test_review_with_negative_budget_keeps_only_lexical_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_judgment(**_: Any) -> Any:
        pytest.fail("A nonpositive budget must not purchase semantic judgments")

    monkeypatch.setattr(relevance, "call_llm_json", unexpected_judgment)
    result = await _ranked_review(
        monkeypatch,
        {
            "low": _candidate("Low", -0.2),
            "high": _candidate("High", 1.5),
            "spare": _candidate("Spare", 0.5),
        },
        count=-1,
    )
    assert result["low"].retrieval_score == 0.0
    assert result["high"].retrieval_score == 1.0
    assert all(
        article.retriever_version == relevance._LEXICAL_ONLY_VERSION for article in result.values()
    )


def test_a_cached_missing_value_sentinel_is_read_as_missing() -> None:
    """One PubMed parser writes "<not found>" where a value is absent, and
    cached metadata still holds it. A truthy DOI every unidentified paper
    shares resolves as a URL and merges distinct papers in the report.
    """
    articles = article_support.build_articles_from_metadata(
        {
            "no-doi": {"title": "A paper with no DOI", "doi": "<not found>"},
            "no-abstract": {"title": "A paper with no abstract", "abstract": "<not found>"},
        },
        "pubmed",
    )

    by_id = {article.source_id: article for article in articles}
    assert by_id["no-doi"].doi is None
    assert by_id["no-abstract"].abstract is None
    # The titles are real metadata and must survive.
    assert by_id["no-doi"].title == "A paper with no DOI"


_FAILED_READ = {
    "status": "failed",
    "records": [],
    "error": "HTTP 503",
}


def test_a_failed_read_is_not_read_as_a_papers_full_text() -> None:
    assert retrieval_support.parse_content_result(json.dumps(_FAILED_READ)) is None
    assert retrieval_support.parse_content_result(_FAILED_READ) is None
    # Ordinary bodies, including JSON documents, are still content.
    assert retrieval_support.parse_content_result("Plain text body") == "Plain text body"
    assert retrieval_support.parse_content_result({"text": "Body"}) == "Body"
    assert retrieval_support.reported_failure({"error": "not the contract"}) is None


@pytest.mark.parametrize(
    "raw",
    [
        {"status": "failed", "records": [], "error": "timeout"},
        json.dumps({"source": "STRING", "query": "WEE1", "records": []}),
    ],
)
def test_a_failed_or_empty_enrichment_lookup_is_no_evidence(raw: Any) -> None:
    from co_scientist.science.generation.literature_review.enrichment import (
        _parse_enrichment_result,
    )

    assert _parse_enrichment_result(raw) == ("", [])
