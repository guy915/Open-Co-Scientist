from __future__ import annotations

import io

import pytest
from co_scientist.domains.documents import ingest as document_ingest
from co_scientist.domains.documents.ingest import _extract_pdf

Line = tuple[str, str, float, float, float]


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[Line]) -> bytes:
    ops = [
        f"BT /{font} {size} Tf {x} {y} Td ({_escape(text)}) Tj ET"
        for text, font, size, x, y in lines
    ]
    return "\n".join(ops).encode("latin-1")


def _page_object(content_num: int, font_nums: dict[str, int]) -> bytes:
    fonts = " ".join(f"/{name} {num} 0 R" for name, num in font_nums.items())
    return (
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 500] "
        f"/Resources << /Font << {fonts} >> >> "
        f"/Contents {content_num} 0 R >>"
    ).encode("latin-1")


def _build_objects(
    pages_lines: list[list[Line]],
) -> tuple[dict[int, bytes], list[int]]:
    font_nums = {"F1": 100, "F2": 101}
    objects: dict[int, bytes] = {
        100: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        101: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    }
    page_nums: list[int] = []
    next_num = 10
    for lines in pages_lines:
        page_num, content_num = next_num, next_num + 1
        next_num += 2
        page_nums.append(page_num)
        stream = _content_stream(lines)
        objects[content_num] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
        )
        objects[page_num] = _page_object(content_num, font_nums)
    kids = " ".join(f"{n} 0 R" for n in page_nums)
    objects[2] = (f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nums)} >>").encode("latin-1")
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    return objects, page_nums


def _serialize(objects: dict[int, bytes]) -> bytes:
    # Compute PDF offsets from real bytes; hand-counted offsets are fragile.
    all_nums = sorted(objects)
    max_num = max(all_nums)
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in all_nums:
        offsets[num] = buf.tell()
        buf.write(f"{num} 0 obj\n".encode("latin-1"))
        buf.write(objects[num])
        buf.write(b"\nendobj\n")
    xref_offset = buf.tell()
    buf.write(f"xref\n0 {max_num + 1}\n".encode("latin-1"))
    buf.write(b"0000000000 65535 f \n")
    for num in range(1, max_num + 1):
        buf.write(f"{offsets.get(num, 0):010d} 00000 n \n".encode("latin-1"))
    buf.write(
        (f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF").encode(
            "latin-1"
        )
    )
    return buf.getvalue()


def build_pdf(pages_lines: list[list[Line]]) -> bytes:
    objects, _ = _build_objects(pages_lines)
    return _serialize(objects)


# Use the real PDF parser so a fake cannot hide missing wiring.


def test_larger_font_infers_a_heading_with_no_outline_or_numbering() -> None:
    # Uniform body lines disambiguate body-size estimation from headings.
    pdf = build_pdf(
        [
            [
                ("Results", "F2", 20, 50, 460),
                ("The assay showed a clear effect.", "F1", 10, 50, 430),
                ("A second sentence of the same paragraph.", "F1", 10, 50, 410),
                ("A third sentence, still body text.", "F1", 10, 50, 390),
            ]
        ]
    )

    text = _extract_pdf(pdf)

    assert "# Results" in text
    assert "The assay showed a clear effect." in text


def test_pdf_page_budget_rejects_whole_upload() -> None:
    pdf = build_pdf([[("Body text", "F1", 10, 50, 450)] for _ in range(251)])

    with pytest.raises(ValueError, match="250 page limit"):
        _extract_pdf(pdf)


def test_pdf_extracted_text_budget_rejects_whole_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = build_pdf([[("A sentence exceeding the configured limit.", "F1", 10, 50, 450)]])
    monkeypatch.setattr(document_ingest, "MAX_PDF_OUTPUT_BYTES", 8)

    with pytest.raises(ValueError, match="5 MiB extracted text limit"):
        document_ingest._extract_pdf_in_process(pdf)
