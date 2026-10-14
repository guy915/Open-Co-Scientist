from __future__ import annotations

import hashlib
import io
import sys
import types
from typing import Any, cast

import pytest
from co_scientist.domains.chat.repository import messages
from co_scientist.domains.research_state.repository import records
from co_scientist.platform.retrieval.run_corpus import (
    engine_context_sources,
)
from fastapi.testclient import TestClient

from tests._client import create_run as _create_run
from tests._client import make_client


def _client_with_run(goal: str) -> tuple[TestClient, str]:
    client = make_client()
    run_id = _create_run(client, goal).json()["id"]
    return client, run_id


def test_pdf_ocr_includes_figures_on_text_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from co_scientist.domains.documents import ingest as document_ingest

    class _Image:
        data = b"x" * 1200
        image = types.SimpleNamespace(width=30, height=40)

    class _Page:
        def __init__(self) -> None:
            self.images = [_Image()]

        def extract_text(self, **_: object) -> str:
            return "Narrative results."

    class _Reader:
        is_encrypted = False

        def __init__(self, _stream: io.BytesIO) -> None:
            self.pages = [_Page()]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=_Reader))
    monkeypatch.setattr(
        document_ingest,
        "_extract_image_ocr",
        lambda _data, **_kwargs: "Plot shows a 42 percent reduction.",
    )

    text = document_ingest._extract_pdf_in_process(b"fake-pdf")

    assert "Narrative results" in text
    assert "[Figure 1.1 sha256=" in text
    assert "42 percent reduction" in text


def test_text_upload_is_extracted_with_immutable_provenance(
    isolated_db: str,
) -> None:
    client, run_id = _client_with_run("Use private kinase evidence")
    content = b"Kinase X inhibition reduced growth in the private assay."

    response = client.post(
        f"/api/runs/{run_id}/attachments/upload",
        files={"file": ("assay.md", content, "text/markdown")},
        data={"consent": "true"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sha256"] == hashlib.sha256(content).hexdigest()
    evidence = records.list_evidence(run_id, db_path=isolated_db)
    uploaded = next(item for item in evidence if item["id"] == payload["id"])
    assert uploaded["abstract"] == content.decode()
    assert uploaded["mime_type"] == "text/markdown"
    assert uploaded["byte_size"] == len(content)
    assert uploaded["document_version"] == payload["sha256"]
    assert uploaded["extraction_tool"] == "utf8-decoder-v1"
    pending = messages.get_pending_steering(run_id, db_path=isolated_db)
    assert pending[-1].meta == {
        "kind": "attachment",
        "evidence_id": payload["id"],
    }


def test_image_upload_is_ocr_extracted_with_multimodal_provenance(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.domains.documents import ingest as document_ingest

    monkeypatch.setattr(
        document_ingest,
        "_extract_image_ocr",
        lambda _data: "Figure 1: Kinase X reduced tumour volume by 42%.",
    )
    client, run_id = _client_with_run("Inspect private figure")

    response = client.post(
        f"/api/runs/{run_id}/attachments/upload",
        files={"file": ("figure.png", b"fixture-image", "image/png")},
        data={"consent": "true"},
    )

    assert response.status_code == 200
    payload = response.json()
    evidence = records.list_evidence(run_id, db_path=isolated_db)
    uploaded = next(item for item in evidence if item["id"] == payload["id"])
    assert "Kinase X" in uploaded["abstract"]
    assert uploaded["mime_type"] == "image/png"
    assert uploaded["extraction_tool"] == "tesseract-cli-v1"


def test_invalid_image_is_rejected_without_persisting_evidence(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from co_scientist.domains.documents import ingest as document_ingest

    def _raise_decode_failure(_data: bytes) -> str:
        raise ValueError("image could not be decoded or OCR failed")

    monkeypatch.setattr(document_ingest, "_extract_image_ocr", _raise_decode_failure)
    client, run_id = _client_with_run("Inspect private figure")

    response = client.post(
        f"/api/runs/{run_id}/attachments/upload",
        files={"file": ("figure.png", b"not-a-real-image", "image/png")},
        data={"consent": "true"},
    )

    assert response.status_code == 422
    assert "OCR failed" in response.json()["detail"]
    assert records.list_evidence(run_id, db_path=isolated_db) == []


def test_engine_context_sources_preserve_private_provenance() -> None:
    rows = [
        {
            "id": "private-1",
            "title": "Unpublished kinase study",
            "source": "attachment",
            "abstract": "Kinase X inhibition reduced AML growth. " * 1000,
        },
        {
            "id": "public-1",
            "title": "Public paper",
            "source": "pubmed",
            "abstract": "Not part of the private corpus.",
        },
    ]

    sources = engine_context_sources(rows, "kinase AML", excerpt_chars=200)

    assert len(sources) == 1
    assert sources[0]["source_type"] == "private_document"
    assert sources[0]["data"]["document_id"] == "private-1"
    assert sources[0]["data"]["private"] is True
    assert len(sources[0]["data"]["excerpt"]) <= 200


_DOC_TEXT = "Tetraploid zebrafish hearts regenerate via klf2a signalling."
_HEADERS = {"X-Client-ID": "doc-scientist"}


def _documents_stage(
    client: TestClient,
    *,
    text: str = _DOC_TEXT,
    name: str = "lab-notes.txt",
    headers: dict[str, str] | None = None,
) -> Any:
    return client.post(
        "/api/documents",
        headers=headers if headers is not None else _HEADERS,
        files={"file": (name, io.BytesIO(text.encode()), "text/plain")},
        data={"consent": "true"},
    )


def _stage_id(client: TestClient, **kwargs: Any) -> str:
    response = _documents_stage(client, **kwargs)
    assert response.status_code == 200, response.text
    return cast(str, response.json()["id"])


@pytest.mark.parametrize(
    ("target", "caller", "status", "interview_status"),
    [
        ("staged", "doc-scientist", 204, 404),
        ("staged", "someone-else", 404, 200),
        ("not-a-real-document", "doc-scientist", 404, 200),
    ],
    ids=["owner", "another-client", "unknown-document"],
)
def test_only_the_owner_can_delete_a_staged_document(
    target: str, caller: str, status: int, interview_status: int
) -> None:
    client = make_client()
    document_id = _stage_id(client)

    response = client.delete(
        f"/api/documents/{document_id if target == 'staged' else target}",
        headers={"X-Client-ID": caller},
    )

    assert response.status_code == status
    interview = client.post(
        "/api/interviews",
        headers=_HEADERS,
        json={"research_challenge": "x", "document_ids": [document_id]},
    )
    assert interview.status_code == interview_status


@pytest.mark.parametrize(
    ("content", "mime_type", "tool", "expected"),
    [
        (
            b"condition,replicate,value\ncontrol,1,4.2\ntreated,1,8.7\n",
            "text/csv",
            "csv-table-v1",
            [
                "[Table 1 rows=2 columns=3]",
                "Header: condition | replicate | value",
                "Row 2: treated | 1 | 8.7",
            ],
        ),
        (
            b'{"assay": {"units": "nM", "values": [3, 5]}}',
            "application/json",
            "json-structure-v1",
            ["[Structured JSON]", '"units": "nM"'],
        ),
    ],
    ids=["csv", "json"],
)
def test_structured_uploads_preserve_their_structure(
    content: bytes, mime_type: str, tool: str, expected: list[str]
) -> None:
    from co_scientist.domains.documents.ingest import extract_document

    document = extract_document(content, mime_type)

    assert document.extraction_tool == tool
    for fragment in expected:
        assert fragment in document.text


@pytest.mark.parametrize(
    ("name", "content", "mime_type"),
    [
        ("figure.png", b"%PDF-1.4\n%mislabeled", "image/png"),
        ("notes.txt", b"\xff\xd8\xff\xe0" + b"\x00" * 32, "text/plain"),
    ],
    ids=["pdf-as-png", "jpeg-as-text"],
)
def test_upload_endpoint_refuses_a_mislabeled_file(
    isolated_db: str, name: str, content: bytes, mime_type: str
) -> None:
    client, run_id = _client_with_run("Mislabeled upload check")

    response = client.post(
        f"/api/runs/{run_id}/attachments/upload",
        files={"file": (name, content, mime_type)},
        data={"consent": "true"},
    )

    assert response.status_code == 422
    assert "does not match" in response.json()["detail"]
    assert records.list_evidence(run_id, db_path=isolated_db) == []


def test_staging_rejects_an_unextractable_document() -> None:
    client = make_client()
    response = client.post(
        "/api/documents",
        headers=_HEADERS,
        files={"file": ("scan.bin", io.BytesIO(b"\x00\x01"), "application/x")},
        data={"consent": "true"},
    )
    assert response.status_code == 422


def test_staged_document_is_not_visible_to_another_client() -> None:
    client = make_client()
    document_id = _stage_id(client)
    response = client.post(
        "/api/interviews",
        headers={"X-Client-ID": "someone-else"},
        json={"research_challenge": "x", "document_ids": [document_id]},
    )
    assert response.status_code == 404


def test_create_run_carries_staged_documents_into_its_corpus() -> None:
    client = make_client()
    document_id = _stage_id(client)
    created = _create_run(
        client,
        "Cardiac regeneration",
        headers=_HEADERS,
        tier="express",
        document_ids=[document_id],
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    evidence = client.get(f"/api/runs/{run_id}/evidence", headers=_HEADERS).json()
    titles = [row["title"] for row in evidence["evidence"]]
    assert "lab-notes.txt" in titles


def test_create_run_with_an_unknown_document_creates_no_run() -> None:
    # Stage uploads before creating runs so partial failure cannot strand an
    # ungrounded draft.
    client = make_client()
    before = client.get("/api/runs", headers=_HEADERS).json()["runs"]
    response = _create_run(
        client,
        "Cardiac regeneration",
        headers=_HEADERS,
        document_ids=["not-a-real-document"],
    )
    assert response.status_code == 404
    after = client.get("/api/runs", headers=_HEADERS).json()["runs"]
    assert len(after) == len(before)


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"title": "Doc", "text": "Some text.", "consent": False}, 422),
        ({"title": "Big", "text": "x" * 200_001, "consent": True}, 422),
    ],
)
def test_attachment_needs_consent_and_a_bounded_size(
    isolated_db: str, body: dict[str, Any], status: int
) -> None:
    client, run_id = _client_with_run("Scientist-in-the-loop goal")

    res = client.post(f"/api/runs/{run_id}/attachments", json=body)

    assert res.status_code == status
