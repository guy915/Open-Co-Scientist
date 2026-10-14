from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Any

from mcp_server.text_extraction import truncate_markdown

MAX_PDF_INPUT_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 250
MAX_PDF_OBJECTS = 50_000
MAX_PDF_DECODED_BYTES = 64 * 1024 * 1024
MAX_PDF_RESPONSE_BYTES = 512 * 1024
MAX_PDF_WALL_SECONDS = 15

PDF_ERROR = "[error: could not extract text from PDF]"
PDF_NO_TEXT = "[note: PDF has no extractable text layer, likely a scan]"
MAX_CONCURRENT_PDF_WORKERS = 2
_pdf_slots = threading.BoundedSemaphore(MAX_CONCURRENT_PDF_WORKERS)


class _PdfBudgetExceededError(ValueError):
    pass


def extract_text_from_pdf(data: bytes, max_chars: int = 50_000) -> str:
    if not _pdf_slots.acquire(blocking=False):
        return PDF_ERROR
    try:
        return _extract_text_admitted(data, max_chars)
    finally:
        _pdf_slots.release()


def _extract_text_admitted(data: bytes, max_chars: int) -> str:
    if len(data) > MAX_PDF_INPUT_BYTES or max_chars < 0:
        return PDF_ERROR
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "mcp_server.pdf_worker", str(max_chars)],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return PDF_ERROR
        try:
            process.communicate(data, timeout=MAX_PDF_WALL_SECONDS)
        except BaseException as exc:
            _kill_worker_group(process.pid)
            process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                return PDF_ERROR
            raise
        finally:
            if process.returncode is None:
                _kill_worker_group(process.pid)
        if process.returncode != 0:
            return PDF_ERROR
        output.seek(0, os.SEEK_END)
        if output.tell() > MAX_PDF_RESPONSE_BYTES:
            return PDF_ERROR
        output.seek(0)
        response = output.read(MAX_PDF_RESPONSE_BYTES + 1)
    try:
        payload = json.loads(response)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return PDF_ERROR
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return PDF_ERROR
    text = payload.get("text")
    if not isinstance(text, str):
        return PDF_ERROR
    if not text.strip():
        return PDF_NO_TEXT
    return truncate_markdown(text, max_chars)


def _kill_worker_group(process_id: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_id, signal.SIGKILL)


def _extract_pdf_in_process(data: bytes, max_chars: int) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF extraction is unavailable") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        object_count = sum(len(objects) for objects in getattr(reader, "xref", {}).values())
        object_count += sum(len(objects) for objects in getattr(reader, "xref_objStm", {}).values())
        if object_count > MAX_PDF_OBJECTS:
            raise _PdfBudgetExceededError("PDF object budget exceeded")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise _PdfBudgetExceededError("PDF page budget exceeded")
        decoded_bytes = 0
        output_chars = 0
        pages: list[str] = []
        for index in range(page_count):
            page: Any = reader.pages[index]
            contents = page.get_contents()
            if contents is not None:
                decoded_bytes += len(contents.get_data())
                if decoded_bytes > MAX_PDF_DECODED_BYTES:
                    raise _PdfBudgetExceededError("PDF decoded-content budget exceeded")
            text = page.extract_text() or ""
            if text.strip():
                remaining = max_chars + 1 - output_chars
                if remaining <= 0:
                    break
                pages.append(text[:remaining])
                output_chars += min(len(text), remaining)
                if len(text) > remaining:
                    break
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF could not be parsed") from exc
    return "\n\n".join(pages)


def _worker_main(max_chars: int) -> int:
    data = sys.stdin.buffer.read(MAX_PDF_INPUT_BYTES + 1)
    try:
        if len(data) > MAX_PDF_INPUT_BYTES:
            raise _PdfBudgetExceededError("PDF input budget exceeded")
        text = _extract_pdf_in_process(data, max_chars)
        payload: dict[str, object] = {"ok": True, "text": text}
    except Exception:
        payload = {"ok": False}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_PDF_RESPONSE_BYTES:
        encoded = b'{"ok": false}'
    sys.stdout.buffer.write(encoded)
    return 0
