from __future__ import annotations

import io
import os
import subprocess

import pytest
from mcp_server import pdf_parser


def _pdf(page_texts: list[str]) -> bytes:
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_numbers = []
    for index, text in enumerate(page_texts):
        page_number = 10 + index * 2
        content_number = page_number + 1
        page_numbers.append(page_number)
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 10 Tf 50 450 Td ({escaped}) Tj ET".encode("latin-1")
        objects[content_number] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 500] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
        ).encode()
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_numbers)} >>".encode()

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = output.tell()
        output.write(f"{number} 0 obj\n".encode())
        output.write(objects[number])
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    max_number = max(objects)
    output.write(f"xref\n0 {max_number + 1}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for number in range(1, max_number + 1):
        output.write(f"{offsets.get(number, 0):010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {max_number + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF".encode()
    )
    return output.getvalue()


def test_extracts_text_from_an_ordinary_pdf() -> None:
    assert "bounded parser control" in pdf_parser.extract_text_from_pdf(
        _pdf(["bounded parser control"])
    )


def test_rejects_a_pdf_over_the_page_budget() -> None:
    result = pdf_parser.extract_text_from_pdf(_pdf(["page"] * 251))

    assert result == "[error: could not extract text from PDF]"


def test_rejects_input_over_the_worker_budget() -> None:
    result = pdf_parser.extract_text_from_pdf(b"%PDF-" + b"x" * (10 * 1024 * 1024))

    assert result == "[error: could not extract text from PDF]"


def test_truncates_extracted_text_to_the_output_budget() -> None:
    result = pdf_parser.extract_text_from_pdf(_pdf(["x" * 50_001]))

    assert result.startswith("x" * 100)
    assert result.endswith("[... truncated for length ...]")


def test_kills_worker_group_when_wall_clock_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    class HungWorker:
        pid = 123
        returncode = None

        def communicate(self, _data: bytes | None = None, timeout: float | None = None) -> None:
            if timeout is not None:
                raise subprocess.TimeoutExpired("pdf worker", timeout)
            self.returncode = -9

    killed: list[int] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: HungWorker())
    monkeypatch.setattr(os, "killpg", lambda pid, _signal: killed.append(pid))

    assert pdf_parser.extract_text_from_pdf(_pdf(["slow"])) == (
        "[error: could not extract text from PDF]"
    )
    assert killed == [123]
