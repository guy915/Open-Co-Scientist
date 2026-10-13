from __future__ import annotations

import contextlib
import csv
import dataclasses
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_TEXT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/tiff", "image/webp"}
MAX_PDF_PAGES = 250
MAX_PDF_OBJECTS = 50_000
MAX_PDF_IMAGES = 30
MAX_PDF_IMAGE_PIXELS = 100_000_000
MAX_PDF_IMAGE_PIXELS_PER_IMAGE = 20_000_000
MAX_PDF_DECODED_BYTES = 64 * 1024 * 1024
MAX_PDF_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_PDF_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PDF_OCR_CALLS = 20
MAX_PDF_OCR_SECONDS = 90
MAX_PDF_WALL_SECONDS = 120

# Text has no reliable magic bytes; validate decoding instead. WEBP needs both
# RIFF and its nonadjacent WEBP tag.
_BINARY_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def _sniff_binary_type(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for signature, mime in _BINARY_SIGNATURES:
        if data.startswith(signature):
            return mime
    return None


def _verify_declared_type(data: bytes, declared_type: str) -> None:
    """Signatures detect mislabeled binary uploads, not valid polyglots or
    threats embedded in genuine documents.
    """
    sniffed = _sniff_binary_type(data)
    if sniffed is not None and sniffed != declared_type:
        raise ValueError(
            f"declared type '{declared_type}' does not match the "
            f"file's actual contents (looks like '{sniffed}')"
        )


@dataclasses.dataclass(frozen=True)
class ExtractedDocument:
    text: str
    mime_type: str
    sha256: str
    byte_size: int
    extraction_tool: str


class _PdfBudgetExceededError(ValueError):
    pass


class _PdfOcrTimeoutError(ValueError):
    pass


def extract_document(data: bytes, mime_type: str) -> ExtractedDocument:
    if not data:
        raise ValueError("uploaded document is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("uploaded document exceeds the 25 MB limit")
    normalized_type = mime_type.split(";", 1)[0].strip().lower()
    _verify_declared_type(data, normalized_type)
    text, tool = _extract_by_type(data, normalized_type)
    if not text.strip():
        raise ValueError("document contains no extractable text")
    return ExtractedDocument(
        text=text,
        mime_type=normalized_type,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        extraction_tool=tool,
    )


def _extract_by_type(data: bytes, normalized_type: str) -> tuple[str, str]:
    if normalized_type in _TEXT_TYPES:
        if normalized_type in {"text/csv", "application/json"}:
            tool = "csv-table-v1" if normalized_type == "text/csv" else "json-structure-v1"
            return _extract_in_worker(data, normalized_type), tool
        return _extract_text_document(data, normalized_type)
    if normalized_type == "application/pdf":
        return _extract_pdf(data), "pypdf-layout+tesseract-fallback-v4"
    if normalized_type in _IMAGE_TYPES:
        return _extract_image_ocr(data), "tesseract-cli-v1"
    raise ValueError(
        "unsupported document type; upload PDF, PNG, JPEG, TIFF, WebP, TXT, Markdown, CSV, or JSON"
    )


def _extract_csv_document(decoded: str) -> tuple[str, str]:
    try:
        rows = list(csv.reader(io.StringIO(decoded)))
    except csv.Error as exc:
        raise ValueError("CSV document could not be parsed") from exc
    if not rows:
        return "", "csv-table-v1"
    width = max(len(row) for row in rows)
    header = rows[0]
    lines = [
        f"[Table 1 rows={len(rows) - 1} columns={width}]",
        "Header: " + " | ".join(header),
    ]
    lines.extend(f"Row {index}: " + " | ".join(row) for index, row in enumerate(rows[1:], start=1))
    return "\n".join(lines), "csv-table-v1"


def _extract_json_document(decoded: str) -> tuple[str, str]:
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON document could not be parsed") from exc
    return (
        "[Structured JSON]\n" + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        "json-structure-v1",
    )


def _extract_text_document(data: bytes, mime_type: str) -> tuple[str, str]:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("text document must be UTF-8") from exc
    if mime_type == "text/csv":
        return _extract_csv_document(decoded)
    if mime_type == "application/json":
        return _extract_json_document(decoded)
    return decoded, "utf8-decoder-v1"


def _extract_pdf(data: bytes) -> str:
    return _extract_in_worker(data, "application/pdf")


def _extract_in_worker(data: bytes, mime_type: str) -> str:
    is_pdf = mime_type == "application/pdf"
    label = "PDF" if is_pdf else "document"
    module = "pdf_worker" if is_pdf else "document_worker"
    args = [] if is_pdf else [mime_type]
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", f"co_scientist.domains.documents.{module}", *args],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=Path(os.path.abspath(__file__)).parents[3],
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except OSError as exc:
            raise ValueError(f"{label} extraction is unavailable") from exc
        try:
            process.communicate(data, timeout=MAX_PDF_WALL_SECONDS)
        except BaseException as exc:
            _kill_pdf_worker_group(process.pid)
            process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ValueError(f"{label} extraction exceeded its 120 second limit") from exc
            raise
        finally:
            _kill_pdf_worker_group(process.pid)
        if process.returncode != 0:
            raise ValueError(f"{label} extraction failed within worker resource limits")
        output.seek(0, os.SEEK_END)
        if output.tell() > MAX_PDF_RESPONSE_BYTES:
            raise ValueError(f"{label} exceeds the 8 MiB worker response limit")
        output.seek(0)
        response = output.read(MAX_PDF_RESPONSE_BYTES + 1)
    try:
        result = json.loads(response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} extraction failed") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        message = result.get("error") if isinstance(result, dict) else None
        raise ValueError(str(message or f"{label} extraction failed"))
    text = result.get("text")
    if not isinstance(text, str):
        raise ValueError(f"{label} extraction failed")
    return text


def _kill_pdf_worker_group(process_id: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_id, signal.SIGKILL)


def _extract_pdf_in_process(data: bytes) -> str:
    budget = _PdfBudget()
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # Fail honestly on a broken deployment.
        raise ValueError("PDF extraction is unavailable") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        object_count = sum(len(objects) for objects in getattr(reader, "xref", {}).values()) + sum(
            len(objects) for objects in getattr(reader, "xref_objStm", {}).values()
        )
        if object_count > MAX_PDF_OBJECTS:
            raise _PdfBudgetExceededError("PDF exceeds the 50000 object limit")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise _PdfBudgetExceededError("PDF exceeds the 250 page limit")
        pages = [reader.pages[index] for index in range(page_count)]
        page_texts = []
        for page in pages:
            get_contents = getattr(page, "get_contents", None)
            contents = get_contents() if get_contents is not None else None
            if contents is not None:
                budget.add_decoded(len(contents.get_data()))
            text = _extract_pdf_page_text(page)
            budget.add_output(text)
            page_texts.append(text)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF could not be parsed") from exc

    page_texts = _apply_heading_markup(reader, pages, page_texts)
    sections = [
        _assemble_pdf_page(index, page, text, budget)
        for index, (page, text) in enumerate(zip(pages, page_texts, strict=True), start=1)
    ]
    result = "\n\n".join(sections)
    if len(result.encode("utf-8")) > MAX_PDF_OUTPUT_BYTES:
        raise _PdfBudgetExceededError("PDF exceeds the 5 MiB extracted text limit")
    return result


@dataclasses.dataclass
class _PdfBudget:
    decoded_bytes: int = 0
    output_bytes: int = 0
    image_count: int = 0
    image_pixels: int = 0
    ocr_calls: int = 0
    ocr_deadline: float = dataclasses.field(
        default_factory=lambda: time.monotonic() + MAX_PDF_OCR_SECONDS
    )

    def add_decoded(self, count: int) -> None:
        self.decoded_bytes += count
        if self.decoded_bytes > MAX_PDF_DECODED_BYTES:
            raise _PdfBudgetExceededError("PDF exceeds the 64 MiB decoded content limit")

    def add_output(self, text: str) -> None:
        self.output_bytes += len(text.encode("utf-8"))
        if self.output_bytes > MAX_PDF_OUTPUT_BYTES:
            raise _PdfBudgetExceededError("PDF exceeds the 5 MiB extracted text limit")

    def begin_ocr(self, seconds: float) -> float:
        self.ocr_calls += 1
        if self.ocr_calls > MAX_PDF_OCR_CALLS:
            raise _PdfBudgetExceededError("PDF exceeds the 20 image OCR limit")
        remaining = self.ocr_deadline - time.monotonic()
        if remaining <= 0:
            raise _PdfBudgetExceededError("PDF exceeded its 90 second aggregate OCR limit")
        return min(seconds, remaining)


def _extract_pdf_page_text(page: Any) -> str:
    try:
        return page.extract_text(extraction_mode="layout") or ""
    except TypeError:
        return page.extract_text() or ""


def _apply_heading_markup(reader: Any, pages: list[Any], page_texts: list[str]) -> list[str]:
    """Heading inference is optional; unsupported outlines or styling must
    fall back to extracted text rather than reject the scientist's
    document.
    """
    from co_scientist.domains.documents.pdf import HeadingBudgetExceededError, apply_heading_markup

    try:
        return apply_heading_markup(reader, pages, page_texts)
    except HeadingBudgetExceededError:
        raise
    except Exception:
        return page_texts


def _assemble_pdf_page(index: int, page: Any, text: str, budget: _PdfBudget | None = None) -> str:
    figure_sections = _extract_pdf_page_figures(index, page, budget)
    page_parts = [f"[Page {index}]", text, *figure_sections]
    return "\n".join(part for part in page_parts if part)


def _extract_pdf_page_figures(index: int, page: Any, budget: _PdfBudget | None = None) -> list[str]:
    """Figures can carry results even on prose-bearing pages; inspect
    substantial embedded images rather than only image-only pages.
    """
    figure_sections = []
    for figure_index, image in enumerate(page.images, start=1):
        if budget is not None:
            budget.image_count += 1
            if budget.image_count > MAX_PDF_IMAGES:
                raise _PdfBudgetExceededError("PDF exceeds the 30 embedded image limit")
            try:
                decoded_image = image.image
                pixels = int(decoded_image.width) * int(decoded_image.height)
            except Exception as exc:
                raise _PdfBudgetExceededError(
                    "PDF embedded image dimensions could not be verified"
                ) from exc
            if pixels > MAX_PDF_IMAGE_PIXELS_PER_IMAGE:
                raise _PdfBudgetExceededError("PDF contains an image exceeding 20 megapixels")
            budget.image_pixels += pixels
            if budget.image_pixels > MAX_PDF_IMAGE_PIXELS:
                raise _PdfBudgetExceededError("PDF exceeds the 100 megapixel decoded image limit")
        image_data = image.data
        if budget is not None:
            budget.add_decoded(len(image_data))
        if len(image_data) < 1024:
            continue
        digest = hashlib.sha256(image_data).hexdigest()
        try:
            if budget is None:
                ocr = _extract_image_ocr(image_data)
            else:
                ocr = _extract_image_ocr(image_data, timeout=budget.begin_ocr(60), pdf_worker=True)
        except (_PdfBudgetExceededError, _PdfOcrTimeoutError):
            raise
        except ValueError:
            figure_sections.append(
                f"[Figure {index}.{figure_index} sha256={digest} OCR unavailable]"
            )
            continue
        if budget is not None:
            budget.add_output(ocr)
        figure_sections.append(f"[Figure {index}.{figure_index} sha256={digest}]\n{ocr}")
    return figure_sections


def _extract_image_ocr(data: bytes, *, timeout: float = 60, pdf_worker: bool = False) -> str:
    if not pdf_worker:
        return _extract_in_worker(data, "image/ocr")
    _verify_image_dimensions(data)
    executable = shutil.which("tesseract")
    if not executable:
        raise ValueError("image OCR is unavailable")
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run(
                [executable, "stdin", "stdout", "--psm", "6"],
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
            if completed.returncode != 0:
                raise ValueError("image could not be decoded or OCR failed")
            output.seek(0, os.SEEK_END)
            if output.tell() > MAX_PDF_OUTPUT_BYTES:
                raise _PdfBudgetExceededError("image exceeds the 5 MiB OCR output limit")
            output.seek(0)
            text = output.read(MAX_PDF_OUTPUT_BYTES + 1)
    except subprocess.TimeoutExpired as exc:
        if pdf_worker:
            raise _PdfOcrTimeoutError("PDF image OCR exceeded its time budget") from exc
        raise ValueError("image OCR is unavailable") from exc
    except OSError as exc:
        raise ValueError("image OCR is unavailable") from exc
    return text.decode("utf-8", errors="replace").strip()


def _verify_image_dimensions(data: bytes) -> None:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "n_frames", 1) > 16:
                raise ValueError("image exceeds the 16 frame limit")
            pixels = 0
            for frame in range(getattr(image, "n_frames", 1)):
                image.seek(frame)
                pixels += image.width * image.height
                if pixels > MAX_PDF_IMAGE_PIXELS_PER_IMAGE:
                    raise ValueError("image exceeds the 20 megapixel decoded dimension limit")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("image dimensions could not be safely verified") from exc


def _document_worker_main(mime_type: str) -> int:
    data = sys.stdin.buffer.read(MAX_UPLOAD_BYTES + 1)
    try:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("uploaded document exceeds the 25 MB limit")
        if mime_type == "image/ocr":
            text = _extract_image_ocr(data, pdf_worker=True)
        elif mime_type in {"text/csv", "application/json"}:
            text, _ = _extract_text_document(data, mime_type)
        else:
            raise ValueError("unsupported worker document type")
        if len(text.encode("utf-8")) > MAX_PDF_OUTPUT_BYTES:
            raise ValueError("document exceeds the 5 MiB extracted text limit")
        payload = {"ok": True, "text": text}
    except ValueError as exc:
        payload = {"ok": False, "error": str(exc)}
    except Exception:
        payload = {"ok": False, "error": "document extraction failed"}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_PDF_RESPONSE_BYTES:
        encoded = b'{"ok":false,"error":"document exceeds the worker response limit"}'
    sys.stdout.buffer.write(encoded)
    return 0


def _pdf_worker_main() -> int:
    data = sys.stdin.buffer.read(MAX_UPLOAD_BYTES + 1)
    try:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("uploaded document exceeds the 25 MB limit")
        text = _extract_pdf_in_process(data)
        payload = {"ok": True, "text": text}
    except ValueError as exc:
        payload = {"ok": False, "error": str(exc)}
    except Exception:
        payload = {"ok": False, "error": "PDF extraction failed"}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_PDF_RESPONSE_BYTES:
        encoded = json.dumps(
            {"ok": False, "error": "PDF exceeds the 8 MiB worker response limit"}
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    return 0
