from __future__ import annotations

import asyncio
import io
import json
import threading
from typing import Any

import pytest
from Bio import Entrez
from fastmcp import FastMCP
from mcp_server import entrez, pdf_parser, pubmed_client, tool_logging, workers
from mcp_server.tools.lit_review import search_pubmed
from mcp_server.workers import run_worker


@pytest.mark.parametrize("count", [0, -1, pubmed_client.MAX_PUBMED_PAPERS + 1, 10**12, True])
def test_pubmed_rejects_bad_counts_before_any_outbound_request(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    def unexpected(*_: Any, **__: Any) -> Any:
        raise AssertionError("outbound work before admission")

    monkeypatch.setattr(search_pubmed, "initialize_entrez", unexpected)
    assert search_pubmed.search_pubmed("query", count)["status"] == "failed"


@pytest.mark.parametrize("count", [0, -1, pubmed_client.MAX_FULLTEXT_PAPERS + 1, 10**12])
async def test_fulltext_rejects_bad_counts_before_cache_or_requests(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    def unexpected() -> Any:
        raise AssertionError("cache work before admission")

    monkeypatch.setattr(search_pubmed, "_pubmed_cache_dir", unexpected)
    assert (await search_pubmed.pubmed_search_with_fulltext("query", "slug", count))[
        "status"
    ] == "failed"


def test_upstream_ids_cannot_expand_metadata_work(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="identifiers"):
        pubmed_client.search_with_relaxation("query", 10, 0, lambda *_: ["1"] * 1000)
    with pytest.raises(ValueError, match="identifier budget"):
        pubmed_client.fetch_pubmed_records(["1"] * (pubmed_client.MAX_PUBMED_PAPERS + 1))


def test_entrez_streaming_bytes_are_bounded_and_response_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = io.BytesIO(b"x" * 11)
    monkeypatch.setattr(entrez, "MAX_ENTREZ_RESPONSE_BYTES", 10)
    monkeypatch.setattr(Entrez, "read", lambda stream: stream.read())
    with pytest.raises(ValueError, match="byte budget"):
        entrez.read_entrez(handle)
    assert handle.closed
    assert entrez.read_entrez(io.BytesIO(b"x" * 10)) == b"x" * 10


def test_pubmed_serialized_response_is_bounded() -> None:
    result = search_pubmed._bounded_result(
        {
            "status": "ok",
            "records": [{"abstract": "x" * (search_pubmed.MAX_PUBMED_RESPONSE_BYTES + 1)}],
        }
    )
    assert result["status"] == "failed"
    assert len(json.dumps(result).encode()) < search_pubmed.MAX_PUBMED_RESPONSE_BYTES
    assert search_pubmed._bounded_result({"status": "ok", "records": []})["status"] == "ok"


async def test_published_pubmed_schemas_include_local_limits() -> None:
    mcp = FastMCP("bounded-tools")
    mcp.tool(search_pubmed.search_pubmed)
    mcp.tool(search_pubmed.pubmed_search_with_fulltext)
    tools = await mcp.list_tools()
    for tool in tools:
        maximum = (
            pubmed_client.MAX_PUBMED_PAPERS
            if tool.name == "search_pubmed"
            else pubmed_client.MAX_FULLTEXT_PAPERS
        )
        schema = tool.parameters["properties"]["max_papers"]
        assert schema["minimum"] == 1
        assert schema["maximum"] == maximum


async def test_mcp_aggregate_admission_releases_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_logging, "_tool_slots", threading.BoundedSemaphore(1))
    entered = asyncio.Event()

    async def wait() -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    wrapped = tool_logging.with_call_logging(wait, "wait")
    task = asyncio.create_task(wrapped())
    await entered.wait()
    assert (await wrapped())["status"] == "failed"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fast = tool_logging.with_call_logging(lambda: "ok", "fast")
    assert fast() == "ok"


def test_pdf_worker_admission_releases_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(pdf_parser, "_pdf_slots", slots)
    assert slots.acquire(blocking=False)
    assert pdf_parser.extract_text_from_pdf(b"pdf") == pdf_parser.PDF_ERROR
    slots.release()

    def fail(*_: Any) -> str:
        raise RuntimeError("parser failed")

    monkeypatch.setattr(pdf_parser, "_extract_text_admitted", fail)
    with pytest.raises(RuntimeError):
        pdf_parser.extract_text_from_pdf(b"pdf")
    assert slots.acquire(blocking=False)
    slots.release()


@pytest.mark.parametrize("cancel_count", [1, 2, 4])
async def test_blocking_worker_holds_admission_through_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch, cancel_count: int
) -> None:
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(tool_logging, "_tool_slots", slots)
    entered = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        entered.set()
        assert release.wait(5)
        return "ok"

    async def call() -> Any:
        return await run_worker(blocking)

    wrapped = tool_logging.with_call_logging(call, "blocking")
    task = asyncio.create_task(wrapped())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert (await wrapped())["status"] == "failed"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert slots.acquire(blocking=False)
    slots.release()


def test_entrez_aggregate_byte_budget_spans_multiple_responses() -> None:
    @entrez.budgeted_entrez
    def read_many() -> None:
        handles = [io.BytesIO(b"a" * 10), io.BytesIO(b"b" * 10)]
        budget = entrez._byte_budget.get()
        assert budget is not None
        budget.remaining = 15
        try:
            entrez.read_entrez_bytes(handles[0])
            entrez.read_entrez_bytes(handles[1])
        finally:
            assert all(handle.closed for handle in handles)

    with pytest.raises(ValueError, match="aggregate byte budget"):
        read_many()


async def test_cancelled_dns_retains_its_own_worker_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(workers, "_dns_slots", slots)
    entered = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        entered.set()
        assert release.wait(5)
        return "ok"

    task = asyncio.create_task(workers.run_dns_worker(blocking))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="DNS capacity"):
            await workers.run_dns_worker(blocking)
    finally:
        release.set()
    assert await asyncio.to_thread(slots.acquire, True, 5)
    slots.release()
