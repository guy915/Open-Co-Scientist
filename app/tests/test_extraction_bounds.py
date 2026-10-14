from __future__ import annotations

import asyncio
import io
import json
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.api import uploads
from co_scientist.domains.documents import extraction_admission, ingest
from co_scientist.platform.db.storage_admission import scoped_peer
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers


def _upload() -> UploadFile:
    return UploadFile(
        io.BytesIO(b"synthetic research notes"),
        filename="notes.txt",
        headers=Headers({"content-type": "text/plain"}),
    )


def _png(width: int = 1, height: int = 1) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


async def test_upload_jobs_are_admitted_before_reading_and_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls: list[bytes] = []

    def extract(data: bytes, mime: str) -> ingest.ExtractedDocument:
        calls.append(data)
        loop.call_soon_threadsafe(started.set)
        if len(calls) == 1:
            assert release.wait(5)
        return ingest.ExtractedDocument("notes", mime, "synthetic", len(data), "synthetic")

    monkeypatch.setattr(ingest, "extract_document", extract)
    first = asyncio.create_task(uploads.extract_upload(_upload()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(HTTPException) as denied:
            await uploads.extract_upload(_upload())
        assert denied.value.status_code == 429
        assert len(calls) == 1
    finally:
        release.set()
        await first


async def test_cancelled_request_keeps_its_parsers_admission_until_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls: list[bytes] = []

    def extract(data: bytes, mime: str) -> ingest.ExtractedDocument:
        calls.append(data)
        loop.call_soon_threadsafe(started.set)
        if len(calls) == 1:
            assert release.wait(5)
        return ingest.ExtractedDocument("notes", mime, "synthetic", len(data), "synthetic")

    real_release = extraction_admission.release

    def release_then_signal(owner: str, peer: str) -> None:
        # The worker releases after the parser returns; signalling from the
        # parser itself let the next request race the release.
        real_release(owner, peer)
        loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(ingest, "extract_document", extract)
    monkeypatch.setattr(extraction_admission, "release", release_then_signal)
    first = asyncio.create_task(uploads.extract_upload(_upload()))
    try:
        await asyncio.wait_for(started.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(HTTPException) as denied:
            await uploads.extract_upload(_upload())
        assert denied.value.status_code == 429
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
    assert (await uploads.extract_upload(_upload())).text == "notes"


def test_image_dimensions_are_rejected_before_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _png(10_000, 10_000)
    calls: list[object] = []
    monkeypatch.setattr(shutil, "which", lambda _: "/synthetic/tesseract")

    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=b"synthetic OCR")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match=r"pixel|dimension|megapixel"):
        ingest._extract_image_ocr(data, pdf_worker=True)
    assert calls == []


def test_standalone_ocr_does_not_capture_unbounded_output_in_api_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    monkeypatch.setattr(shutil, "which", lambda _: "/synthetic/tesseract")

    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"synthetic OCR")

    monkeypatch.setattr(subprocess, "run", run)

    def popen(*args: Any, **kwargs: Any) -> SimpleNamespace:
        workers.append(kwargs)
        assert kwargs["stdout"] != subprocess.PIPE
        kwargs["stdout"].write(json.dumps({"ok": True, "text": "synthetic OCR"}).encode())
        return SimpleNamespace(pid=1, returncode=0, communicate=lambda *a, **k: (None, None))

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(ingest, "_kill_pdf_worker_group", lambda _: None)
    ingest._extract_image_ocr(_png())
    assert not any(call.get("capture_output") for call in calls)
    assert len(workers) == 1
    assert workers[0]["start_new_session"]
    assert workers[0]["env"]["OMP_THREAD_LIMIT"] == "1"


@pytest.mark.parametrize("boundary", ["owner", "peer", "global"])
async def test_identity_rotation_cannot_bypass_extraction_admission(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    release = threading.Event()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def extract(data: bytes, mime: str) -> ingest.ExtractedDocument:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return ingest.ExtractedDocument("notes", mime, "synthetic", len(data), "synthetic")

    async def upload(owner: str, peer: str) -> ingest.ExtractedDocument:
        with scoped_peer(peer):
            return await uploads.extract_upload(_upload(), owner=owner)

    monkeypatch.setattr(ingest, "extract_document", extract)
    tasks = [asyncio.create_task(upload("owner-1", "peer-1"))]
    try:
        await asyncio.wait_for(started.wait(), 2)
        if boundary == "global":
            started.clear()
            tasks.append(asyncio.create_task(upload("owner-2", "peer-2")))
            await asyncio.wait_for(started.wait(), 2)
        owner = "owner-1" if boundary == "owner" else "owner-3"
        peer = "peer-1" if boundary == "peer" else "peer-3"
        with pytest.raises(HTTPException) as denied:
            await upload(owner, peer)
        assert denied.value.status_code == 429
    finally:
        release.set()
        await asyncio.gather(*tasks)


def test_ocr_output_is_read_with_a_finite_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/synthetic/tesseract")
    monkeypatch.setattr(ingest, "MAX_PDF_OUTPUT_BYTES", 16)

    def run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        assert not kwargs.get("capture_output")
        kwargs["stdout"].write(b"x" * 17)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="OCR output limit"):
        ingest._extract_image_ocr(_png(), pdf_worker=True)


@pytest.mark.skipif(sys.platform != "linux", reason="observes Linux worker limits through procfs")
def test_disposable_worker_applies_limits_before_accepting_document_bytes() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "co_scientist.domains.documents.document_worker", "image/ocr"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        observed = ""
        while time.monotonic() < deadline:
            assert process.poll() is None
            observed = Path(f"/proc/{process.pid}/limits").read_text()
            if "536870912" in observed:
                break
            time.sleep(0.01)
        assert "536870912" in observed
        for label, value in (
            ("Max cpu time", "115"),
            ("Max file size", "8388608"),
            ("Max open files", "64"),
        ):
            line = next(row for row in observed.splitlines() if row.startswith(label))
            assert line[len(label) :].split()[:2] == [value, value]
    finally:
        process.kill()
        process.communicate()
