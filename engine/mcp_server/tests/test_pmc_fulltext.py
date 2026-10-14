from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from mcp_server.literature_review import PMC_BATCH_SIZE, PubmedSource
from mcp_server.pmc_articles import split_pmc_articles
from mcp_server.tests._entrez import (
    CannedEntrezHandle,
    efetch_by_id,
    elink_by_id,
    install_entrez,
)
from mcp_server.tools.lit_review import search_pubmed as tool

_HEAD = '<?xml version="1.0"  ?><!DOCTYPE pmc-articleset><pmc-articleset>'


def _article(pmc_id: str, text: str | None = None) -> str:
    # Shaped like PMC's answer: a sub-article and a citation carry other IDs.
    return (
        '<article article-type="research-article"><front><article-meta>'
        f'<article-id pub-id-type="pmcid">PMC{pmc_id}</article-id>'
        f'<article-id pub-id-type="pmcaid">{pmc_id}</article-id>'
        "<article-title>Title</article-title></article-meta></front>"
        f"<body><sec><title>Results</title><p>{text or f'Body of {pmc_id}.'}</p></sec></body>"
        '<back><ref-list><ref><element-citation><pub-id pub-id-type="pmcid">PMC1</pub-id>'
        "</element-citation></ref></ref-list></back>"
        '<sub-article><front-stub><article-id pub-id-type="pmcid">PMC2</article-id>'
        "</front-stub></sub-article></article>"
    )


def _articleset(*parts: str) -> bytes:
    return (_HEAD + "".join(parts) + "</pmc-articleset>").encode()


class _Pmc:
    """Answers `efetch db=pmc` from canned bodies and records each request's IDs."""

    def __init__(self, batch: dict[str, bytes], alone: dict[str, bytes] | None = None) -> None:
        self.batch = batch
        self.alone = alone or {}
        self.requests: list[str] = []

    def efetch(self, **kwargs: Any) -> BytesIO:
        assert kwargs["db"] == "pmc"
        ids = str(kwargs["id"])
        self.requests.append(ids)
        if "," not in ids and ids in self.alone:
            return BytesIO(self.alone[ids])
        return BytesIO(self.batch[ids])


def _install(monkeypatch: pytest.MonkeyPatch, pmc: _Pmc) -> None:
    install_entrez(monkeypatch, efetch=pmc.efetch)


def test_one_efetch_serves_every_article_of_a_batch_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PMC answers in its own order, not the request's.
    pmc = _Pmc({"910,930": _articleset(_article("930"), _article("910"))})
    _install(monkeypatch, pmc)

    texts = PubmedSource(Path())._download_pmc_fulltexts(["910", "930", "910"])

    assert pmc.requests == ["910,930"]
    assert set(texts) == {"910", "930"}
    assert "Body of 910." in texts["910"] and "Body of 930." not in texts["910"]
    assert "Body of 930." in texts["930"] and "Body of 910." not in texts["930"]


def test_ids_beyond_the_batch_size_go_in_a_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [str(100 + index) for index in range(PMC_BATCH_SIZE + 1)]
    first, second = ",".join(ids[:-1]), ids[-1]
    pmc = _Pmc(
        {
            first: _articleset(*(_article(pmc_id) for pmc_id in ids[:-1])),
            second: _articleset(_article(second)),
        }
    )
    _install(monkeypatch, pmc)

    texts = PubmedSource(Path())._download_pmc_fulltexts(ids)

    assert pmc.requests == [first, second]
    assert set(texts) == set(ids)


def test_an_article_split_across_responses_is_fetched_alone_not_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cut = _article("930", "Whole second article.")
    split = (_HEAD + _article("910") + cut[: len(cut) // 2] + "Result too long").encode()
    pmc = _Pmc({"910,930": split}, alone={"930": _articleset(cut)})
    _install(monkeypatch, pmc)

    texts = PubmedSource(Path())._download_pmc_fulltexts(["910", "930"])

    assert pmc.requests == ["910,930", "930"]
    assert "Body of 910." in texts["910"] and "930" not in texts["910"]
    assert "Whole second article." in texts["930"] and "Body of 910." not in texts["930"]


def test_an_article_missing_from_the_answer_is_fetched_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pmc = _Pmc(
        {"910,920,930": _articleset(_article("910"), _article("930"))},
        alone={"920": _articleset(_article("920"))},
    )
    _install(monkeypatch, pmc)

    with caplog.at_level(logging.WARNING, logger="mcp_server"):
        texts = PubmedSource(Path())._download_pmc_fulltexts(["910", "920", "930"])

    assert pmc.requests == ["910,920,930", "920"]
    assert "Body of 920." in texts["920"]
    assert "fetching them alone" in caplog.text


def test_an_article_pmc_reports_unavailable_is_not_fetched_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = '<error id="920">The following PMCID is not available: 920</error>'
    pmc = _Pmc({"910,920": _articleset(error, _article("910"))})
    _install(monkeypatch, pmc)

    with caplog.at_level(logging.WARNING, logger="mcp_server"):
        texts = PubmedSource(Path())._download_pmc_fulltexts(["910", "920"])

    assert pmc.requests == ["910,920"]
    assert set(texts) == {"910"}
    assert "no full text for 1 articles" in caplog.text


def test_split_keeps_neither_copy_of_an_id_claimed_twice_or_an_unrequested_one() -> None:
    body = _articleset(
        _article("910", "first"), _article("910", "second"), _article("930"), _article("999")
    ).decode()

    assert set(split_pmc_articles(body, ["910", "930"])) == {"930"}


def test_split_ignores_an_article_without_its_own_pmc_id() -> None:
    anonymous = _article("910").replace('pub-id-type="pmcid">PMC910', 'pub-id-type="doi">10.1/x')

    assert split_pmc_articles(_articleset(anonymous).decode(), ["910"]) == {}


def test_a_failed_batch_is_logged_once_and_not_retried_article_by_article(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[str] = []

    def efetch(**kwargs: Any) -> BytesIO:
        requests.append(str(kwargs["id"]))
        raise RuntimeError("PMC unavailable")

    install_entrez(monkeypatch, efetch=efetch)

    with caplog.at_level(logging.ERROR, logger="mcp_server"):
        assert PubmedSource(Path())._download_pmc_fulltexts(["910", "930"]) == {}

    assert requests == ["910,930"]
    assert "batch of 2 failed" in caplog.text


def _pubmed_article(paper_id: str) -> dict[str, Any]:
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


def test_the_fulltext_search_attaches_each_papers_own_text_and_skips_cached_articles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    links = {"10": "910", "20": "920", "30": "930"}
    pmc_requests: list[str] = []
    bodies = {
        "910,920,930": _articleset(_article("930"), _article("910"), _article("920")),
        "940": _articleset(_article("940")),
    }

    def efetch(**kwargs: Any) -> Any:
        if kwargs.get("db") == "pmc":
            pmc_requests.append(str(kwargs["id"]))
            return BytesIO(bodies[str(kwargs["id"])])
        return efetch_by_id(_pubmed_article)(**kwargs)

    def install(search_ids: list[str]) -> None:
        install_entrez(
            monkeypatch,
            esearch=lambda **_kwargs: CannedEntrezHandle({"IdList": search_ids}),
            efetch=efetch,
            elink=elink_by_id(links.get),
        )

    install(["10", "20", "30"])
    first = asyncio.run(tool.pubmed_search_with_fulltext("query", "slug", max_papers=3))

    assert first["status"] == "ok"
    assert pmc_requests == ["910,920,930"]
    assert [record["source_id"] for record in first["records"]] == ["10", "20", "30"]
    for record in first["records"]:
        own = links[record["source_id"]]
        assert f"Body of {own}." in record["fulltext"]
        assert all(
            f"Body of {other}." not in record["fulltext"]
            for other in links.values()
            if other != own
        )

    links["40"] = "940"
    install(["20", "40"])
    second = asyncio.run(tool.pubmed_search_with_fulltext("query", "slug", max_papers=2))

    assert second["status"] == "ok"
    assert pmc_requests == ["910,920,930", "940"]
    assert [record["source_id"] for record in second["records"]] == ["20", "40"]
