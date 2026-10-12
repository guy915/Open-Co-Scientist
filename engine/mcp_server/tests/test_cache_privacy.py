from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp_server.cache_privacy import PUBLIC_PAPERS, purge_legacy_pubmed_cache
from mcp_server.literature_review import PubmedSource
from mcp_server.tools.lit_review import search_pubmed as tool


def _metadata(paper: str) -> dict[str, Any]:
    return {
        "title": f"Public paper {paper}",
        "abstract": "Public abstract",
        "pmc_full_text_id": paper,
        "authors": [],
    }


def test_requests_cache_public_papers_without_queries_topics_or_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    monkeypatch.setattr(
        PubmedSource,
        "pubmed_search_ids",
        lambda self, query, **kw: ["123"] if query == "PRIVATE_QUERY_ONE" else ["456"],
    )
    monkeypatch.setattr(
        PubmedSource,
        "_fetch_papers_details",
        lambda self, papers: {paper: _metadata(paper) for paper in papers},
    )
    monkeypatch.setattr(
        PubmedSource,
        "_download_pmc_fulltexts",
        lambda self, papers: {
            paper: f"<article><abstract><p>Public text {paper}</p></abstract></article>"
            for paper in papers
        },
    )
    for query, slug, run, paper in [
        ("PRIVATE_QUERY_ONE", "PRIVATE_TOPIC_ONE", "PRIVATE_RUN_ONE", "123"),
        ("PRIVATE_QUERY_TWO", "PRIVATE_TOPIC_TWO", "PRIVATE_RUN_TWO", "456"),
    ]:
        result = asyncio.run(
            tool.pubmed_search_with_fulltext(query, slug, max_papers=2, run_id=run)
        )
        assert result["status"] == "ok"
        assert len(result["records"]) == 1
        assert result["records"][0]["source_id"] == paper
        assert f"Public text {paper}" in result["records"][0]["fulltext"]
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert {p.name for p in files} == {
        "123.metadata.json",
        "123.fulltext.html",
        "456.metadata.json",
        "456.fulltext.html",
    }
    assert all(not p.is_symlink() for p in tmp_path.rglob("*"))
    assert "PRIVATE_" not in str(list(tmp_path.rglob("*")))
    assert all(b"PRIVATE_" not in p.read_bytes() for p in files)


def test_empty_and_failed_searches_do_not_write_private_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    monkeypatch.setattr(PubmedSource, "pubmed_search_ids", lambda *args, **kw: [])
    result = asyncio.run(
        tool.pubmed_search_with_fulltext("PRIVATE_QUERY", "PRIVATE_TOPIC", run_id="PRIVATE_RUN")
    )
    assert result["status"] == "ok" and result["records"] == []
    assert not list(tmp_path.rglob("*.json"))
    monkeypatch.setattr(PubmedSource, "pubmed_search_ids", lambda *args, **kw: ["123"])

    def fail(*args: Any) -> dict[str, Any]:
        raise RuntimeError("metadata failure")

    monkeypatch.setattr(PubmedSource, "_fetch_papers_details", fail)
    failed = asyncio.run(
        tool.pubmed_search_with_fulltext("PRIVATE_QUERY", "PRIVATE_TOPIC", run_id="PRIVATE_RUN")
    )
    assert failed["status"] == "failed" and failed["records"] == []
    assert "PRIVATE_" not in str(list(tmp_path.rglob("*")))


def test_migration_erases_legacy_associations_and_preserves_public_cache_and_outside_files(
    tmp_path: Path,
) -> None:
    pubmed = tmp_path / "cache" / "pubmed"
    legacy = pubmed / "PRIVATE_TOPIC" / "runs" / "PRIVATE_RUN"
    legacy.mkdir(parents=True)
    (legacy / ".manifest.json").write_text(
        json.dumps({"query": "PRIVATE_QUERY", "run_id": "PRIVATE_RUN"})
    )
    public = pubmed / PUBLIC_PAPERS / "shared"
    public.mkdir(parents=True)
    (public / "123.metadata.json").write_text(json.dumps(_metadata("123")))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("unrelated")
    (pubmed / "PRIVATE_TOPIC_LINK").symlink_to(outside, target_is_directory=True)
    assert purge_legacy_pubmed_cache(tmp_path / "cache") == 2
    assert list(pubmed.iterdir()) == [pubmed / PUBLIC_PAPERS]
    assert (public / "123.metadata.json").exists()
    assert (outside / "keep.txt").read_text() == "unrelated"
    assert purge_legacy_pubmed_cache(tmp_path / "cache") == 0


def test_migration_refuses_a_symlinked_pubmed_root(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("unrelated")
    (root / "pubmed").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        purge_legacy_pubmed_cache(root)
    assert (outside / "keep.txt").exists()


def test_server_erases_legacy_cache_before_accepting_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_server.server import app

    legacy = tmp_path / "pubmed" / "PRIVATE_TOPIC" / "runs" / "PRIVATE_RUN"
    legacy.mkdir(parents=True)
    (legacy / ".manifest.json").write_text("PRIVATE_QUERY")
    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert not legacy.exists()


def test_migration_failure_prevents_serving_and_omits_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_server import cache_privacy
    from mcp_server.server import app

    monkeypatch.setenv("COSCIENTIST_LIT_REVIEW_DIR", str(tmp_path))

    def fail(root: Path) -> int:
        raise PermissionError("PRIVATE_TOPIC PRIVATE_QUERY")

    monkeypatch.setattr(cache_privacy, "purge_legacy_pubmed_cache", fail)
    with (
        pytest.raises(RuntimeError, match="Legacy literature cache cleanup failed") as error,
        TestClient(app),
    ):
        pytest.fail("startup must refuse this cache")
    assert "PRIVATE_" not in str(error.value)
