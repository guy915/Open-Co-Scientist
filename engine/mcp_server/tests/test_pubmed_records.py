import asyncio
from pathlib import Path
from typing import Any

import pytest
from Bio.Entrez.Parser import StringElement
from mcp_server.pubmed_client import _EntrezClient
from mcp_server.tests._entrez import (
    CannedEntrezHandle,
    efetch_by_id,
    elink_by_id,
    install_entrez,
)
from mcp_server.tools.lit_review import search_pubmed


@pytest.mark.parametrize("abstract", [None, ["<b>Methods:</b>", "Two parts."]])
@pytest.mark.parametrize("doi", [None, "10.1234/paper"])
def test_both_pubmed_entrypoints_share_optional_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, abstract: list[str] | None, doi: str | None
) -> None:
    article: dict[str, Any] = {
        "ArticleTitle": "<i>Paper</i>",
        "AuthorList": [{"ForeName": "Ada", "LastName": "Lovelace"}, {"CollectiveName": "Team"}],
        "Journal": {"Title": "Journal", "JournalIssue": {"PubDate": {"Year": "2024"}}},
        "PublicationTypeList": ["Journal Article"],
    }
    if abstract is not None:
        article["Abstract"] = {"AbstractText": abstract}
    raw = {
        "MedlineCitation": {
            "Article": article,
            "DateRevised": {"Year": "2025", "Month": "2", "Day": "1"},
        },
        "PubmedData": {
            "ArticleIdList": [StringElement(doi, "ArticleId", {"IdType": "doi"}, "ArticleId")]
            if doi
            else [],
        },
    }
    install_entrez(
        monkeypatch,
        esearch=lambda **_kwargs: CannedEntrezHandle({"IdList": ["123"]}),
        efetch=efetch_by_id(lambda _paper_id: {"PubmedArticle": [raw]}),
        elink=elink_by_id(lambda _paper_id: None),
    )
    fulltext = _EntrezClient(tmp_path)._fetch_papers_details(["123"])["123"]
    result = search_pubmed.search_pubmed("query", max_papers=1)
    assert fulltext is not None and result["status"] == "ok"
    metadata = search_pubmed.Article(**result["records"][0])
    expected_abstract = "Methods: Two parts." if abstract else None
    assert (fulltext["title"], metadata.title) == ("Paper", "Paper")
    assert (fulltext["abstract"], metadata.abstract) == (expected_abstract, expected_abstract)
    assert fulltext["authors"] == metadata.authors == ["Ada Lovelace"]
    assert fulltext["publication"] == metadata.venue == "Journal"
    assert fulltext["doi"] == doi
    assert metadata.url == (
        f"https://doi.org/{doi}" if doi else "https://pubmed.ncbi.nlm.nih.gov/123/"
    )
    assert metadata.year == 2024
    assert fulltext["date_revised"] == "2025/2/1"
    assert fulltext["publication_types"] == ["Journal Article"]


_BOOK_RECORD: dict[str, list[Any]] = {
    "PubmedArticle": [],
    "PubmedBookArticle": [{"BookDocument": {}}],
}


def _journal_record(title: str) -> dict[str, Any]:
    return {
        "PubmedArticle": [
            {
                "MedlineCitation": {
                    "Article": {
                        "ArticleTitle": title,
                        "AuthorList": [],
                        "Journal": {"Title": "Journal", "JournalIssue": {}},
                    },
                    "DateRevised": {"Year": "2025", "Month": "2", "Day": "1"},
                },
                "PubmedData": {"ArticleIdList": []},
            }
        ]
    }


def test_a_book_record_does_not_fail_a_pubmed_search(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entrez(
        monkeypatch,
        esearch=lambda **_kwargs: CannedEntrezHandle({"IdList": ["111", "222"]}),
        efetch=efetch_by_id(
            lambda paper_id: _BOOK_RECORD if paper_id == "222" else _journal_record("Kept paper")
        ),
    )

    result = search_pubmed.search_pubmed("query", max_papers=2)

    assert result["status"] == "ok"
    assert [record["title"] for record in result["records"]] == ["Kept paper"]


def test_a_book_record_has_no_paper_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_entrez(monkeypatch, efetch=efetch_by_id(lambda _paper_id: _BOOK_RECORD))

    assert _EntrezClient(tmp_path)._fetch_papers_details(["222"]) == {"222": None}


_UNKNOWN_RESPONSES: list[Any] = [
    {},
    {"PubmedArticle": []},
    {"PubmedArticle": [], "PubmedBookArticle": []},
    {"ERROR": "upstream detail"},
]


def _install_mixed_search(monkeypatch: pytest.MonkeyPatch, second: Any) -> None:
    install_entrez(
        monkeypatch,
        esearch=lambda **_kwargs: CannedEntrezHandle({"IdList": ["111", "222"]}),
        efetch=efetch_by_id(
            lambda paper_id: second if paper_id == "222" else _journal_record("Kept paper")
        ),
        elink=elink_by_id(lambda _paper_id: None),
    )


@pytest.mark.parametrize("response", _UNKNOWN_RESPONSES)
def test_a_response_without_an_article_or_book_fails_the_pubmed_search(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    _install_mixed_search(monkeypatch, response)

    result = search_pubmed.search_pubmed("query", max_papers=2)

    assert result["status"] == "failed"
    assert "upstream detail" not in str(result)


@pytest.mark.parametrize("response", _UNKNOWN_RESPONSES)
def test_a_response_without_an_article_or_book_has_no_paper_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: Any
) -> None:
    install_entrez(monkeypatch, efetch=lambda **_kwargs: CannedEntrezHandle(response))

    with pytest.raises(ValueError, match="no article record"):
        _EntrezClient(tmp_path)._fetch_papers_details(["222"])


def test_a_book_record_does_not_fail_the_fulltext_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    _install_mixed_search(monkeypatch, _BOOK_RECORD)

    result = asyncio.run(search_pubmed.pubmed_search_with_fulltext("query", "topic", max_papers=2))

    assert result["status"] == "ok"
    assert {path.name for path in tmp_path.rglob("*.metadata.json")} == {"111.metadata.json"}


@pytest.mark.parametrize("response", _UNKNOWN_RESPONSES)
def test_a_response_without_an_article_or_book_fails_the_fulltext_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: Any
) -> None:
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    _install_mixed_search(monkeypatch, response)

    result = asyncio.run(search_pubmed.pubmed_search_with_fulltext("query", "topic", max_papers=2))

    assert result["status"] == "failed"
    assert "upstream detail" not in str(result)
