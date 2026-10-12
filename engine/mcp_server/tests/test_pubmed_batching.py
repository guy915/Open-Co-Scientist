import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp_server.pubmed_client import EUTILS_BATCH_SIZE, fetch_pmc_ids
from mcp_server.tests._entrez import (
    CannedEntrezHandle,
    efetch_by_id,
    elink_by_id,
    install_entrez,
)
from mcp_server.tools.lit_review import search_pubmed


def _article(paper_id: str) -> dict[str, Any]:
    return {
        "PubmedArticle": [
            {
                "MedlineCitation": {
                    "Article": {
                        "ArticleTitle": f"Paper {paper_id}",
                        "Journal": {"Title": "Journal", "JournalIssue": {}},
                    },
                },
                "PubmedData": {"ArticleIdList": []},
            }
        ]
    }


class _Requests:
    def __init__(self) -> None:
        self.efetch: list[list[str]] = []
        self.elink: list[dict[str, Any]] = []


def _install(
    monkeypatch: pytest.MonkeyPatch,
    search_ids: list[str],
    pmc: dict[str, str] | None = None,
    reverse_answers: bool = True,
) -> _Requests:
    requests = _Requests()
    fetch = efetch_by_id(_article)
    link = elink_by_id(lambda paper_id: (pmc or {}).get(paper_id))

    def efetch(**kwargs: Any) -> CannedEntrezHandle:
        requests.efetch.append(list(kwargs["id"]))
        answer = fetch(**kwargs)
        if reverse_answers:
            # PubMed answers in its own order, not the request's.
            answer.payload["PubmedArticle"].reverse()
        return answer

    def elink(**kwargs: Any) -> CannedEntrezHandle:
        requests.elink.append(kwargs)
        answer = link(**kwargs)
        answer.payload.reverse()
        return answer

    install_entrez(
        monkeypatch,
        esearch=lambda **_kwargs: CannedEntrezHandle({"IdList": search_ids}),
        efetch=efetch,
        elink=elink,
    )
    return requests


def test_metadata_for_every_hit_is_one_efetch_in_search_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install(monkeypatch, ["30", "10", "30", "20"])

    result = search_pubmed.search_pubmed("query", max_papers=4)

    assert result["status"] == "ok"
    assert [record["source_id"] for record in result["records"]] == ["30", "10", "20"]
    assert [record["title"] for record in result["records"]] == [
        "Paper 30",
        "Paper 10",
        "Paper 20",
    ]
    assert requests.efetch == [["30", "10", "20"]]
    assert requests.elink == []


def test_a_large_id_list_is_split_at_the_eutils_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [str(value) for value in range(1, EUTILS_BATCH_SIZE + 3)]
    requests = _install(monkeypatch, ids)

    result = search_pubmed.search_pubmed("query", max_papers=len(ids))

    assert result["status"] == "ok"
    assert [len(batch) for batch in requests.efetch] == [EUTILS_BATCH_SIZE, 2]
    assert [record["source_id"] for record in result["records"]] == ids


def test_fulltext_search_batches_metadata_and_pmc_links_and_skips_cached_papers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    pmc = {"10": "910", "30": "930"}
    requests = _install(monkeypatch, ["10", "20", "30"], pmc)

    first = asyncio.run(
        search_pubmed.pubmed_search_with_fulltext(
            "query", "slug", max_papers=3, include_fulltext=False
        )
    )

    assert first["status"] == "ok"
    assert requests.efetch == [["10", "20", "30"]]
    (link_request,) = requests.elink
    assert link_request["id"] == ["10", "20", "30"]
    assert link_request["linkname"] == "pubmed_pmc"
    records = {record["source_id"]: record for record in first["records"]}
    assert {paper: records[paper]["pmc_full_text_id"] for paper in records} == {
        "10": "910",
        "20": None,
        "30": "930",
    }

    second_requests = _install(monkeypatch, ["10", "40", "20"], {"40": "940"})
    second = asyncio.run(
        search_pubmed.pubmed_search_with_fulltext(
            "query", "slug", max_papers=3, include_fulltext=False
        )
    )

    assert second["status"] == "ok"
    assert second_requests.efetch == [["40"]]
    assert [request["id"] for request in second_requests.elink] == [["40"]]


def test_a_paper_pubmed_did_not_return_fails_the_fulltext_search(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    install_entrez(
        monkeypatch,
        esearch=lambda **_kwargs: CannedEntrezHandle({"IdList": ["10", "20"]}),
        efetch=efetch_by_id(lambda paper_id: _article(paper_id) if paper_id == "10" else {}),
        elink=elink_by_id(lambda _paper_id: None),
    )

    result = asyncio.run(
        search_pubmed.pubmed_search_with_fulltext(
            "query", "slug", max_papers=2, include_fulltext=False
        )
    )

    assert result["status"] == "failed"


def test_a_citing_pmc_article_is_not_taken_for_the_papers_own_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_sets = [
        {
            "IdList": ["10"],
            "LinkSetDb": [{"LinkName": "pubmed_pmc_refs", "Link": [{"Id": "777"}]}],
        },
        {
            "IdList": ["20"],
            "LinkSetDb": [{"LinkName": "pubmed_pmc", "Link": [{"Id": "920"}]}],
        },
    ]
    install_entrez(monkeypatch, elink=lambda **_kwargs: CannedEntrezHandle(link_sets))

    assert fetch_pmc_ids(["10", "20"]) == {"10": None, "20": "920"}


def test_a_failed_pmc_lookup_leaves_papers_without_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def elink(**_kwargs: Any) -> CannedEntrezHandle:
        raise RuntimeError("ELink unavailable")

    install_entrez(monkeypatch, elink=elink)

    assert fetch_pmc_ids(["10", "20"]) == {"10": None, "20": None}
