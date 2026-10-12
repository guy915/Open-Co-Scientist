from __future__ import annotations

import io
import json
import logging
import socket
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from evaluations import benchmark_artifact_safety as safety
from evaluations import paired_artifacts, paired_quality
from evaluations._paired_db import read_snapshot
from evaluations.tests.test_paired_artifacts import cohort


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access in artifact safety tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def test_actual_httpx_query_log_is_unsafe_without_console_masking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = io.StringIO()
    logger = logging.Logger("historical-httpx", level=logging.INFO)
    logger.addHandler(logging.StreamHandler(captured))
    monkeypatch.setattr(httpx._client, "logger", logger)
    credential = 'fixture-only/plus+"quote'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        client.get("https://api.openalex.org/works", params={"api_key": credential})
    log = tmp_path / "mcp.log"
    log.write_text(captured.getvalue())
    assert quote(credential, safe="") in log.read_text()
    with pytest.raises(ValueError, match="configured credential") as error:
        safety.check_artifacts([log], [credential])
    assert credential not in str(error.value)
    assert quote(credential, safe="") not in str(error.value)


@pytest.mark.parametrize("wire", ["literal", "url", "json"])
def test_database_credential_is_rejected_without_modifying_it(tmp_path: Path, wire: str) -> None:
    credential = 'fixture-only/plus+"quote'
    if wire == "url":
        value = str(
            httpx.Request(
                "GET", "https://api.openalex.org/works", params={"api_key": credential}
            ).url
        )
    elif wire == "json":
        value = json.dumps({"api_key": credential})
    else:
        value = credential
    db = tmp_path / "snapshot.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE messages(body TEXT)")
        connection.execute("INSERT INTO messages VALUES (?)", (value,))
    before = db.read_bytes()
    with pytest.raises(ValueError, match="configured credential"):
        safety.check_artifacts([db], [credential])
    assert db.read_bytes() == before


def test_credential_crossing_chunk_boundary_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db-wal"
    credential = "fixture-only/plus+quote"
    db.write_bytes(b"x" * (safety._CHUNK_BYTES - 4) + credential.encode())
    with pytest.raises(ValueError, match="configured credential"):
        safety.check_artifacts([db], [credential])


def test_clean_outputs_pass_without_changes_and_missing_output_is_allowed(tmp_path: Path) -> None:
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    db = prepared / "snapshot.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE receipts(count INTEGER)")
        connection.execute("INSERT INTO receipts VALUES (6)")
    report = tmp_path / "paired.md"
    report.write_text("n=3 goal pairs. Quality remains inconclusive.\n")
    before = db.read_bytes(), report.read_bytes()
    safety.check_artifacts([prepared, report, tmp_path / "missing.json"], ["fixture-only"])
    assert (db.read_bytes(), report.read_bytes()) == before


def test_symbolic_links_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "outputs"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.write_text("fixture-only")
    (root / "alias").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic links"):
        safety.check_artifacts([root], [])


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_prepared_pair_credentials_block_judging_even_after_failure_or_cancellation(
    tmp_path: Path, status: str
) -> None:
    manifest = paired_artifacts.prepare(cohort(tmp_path), tmp_path / "paired-artifacts")
    directory = manifest.parent / "main-0"
    db = directory / "snapshot.db"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE runs SET status=?", (status,))
        connection.execute(
            "UPDATE claim_evidence SET claim=? WHERE claim='claim one'",
            ("fixture-only/plus+quote",),
        )
    receipt_path = directory / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["metrics"] = read_snapshot(db, "r", "cell-biology", "a" * 40).metrics
    receipt_path.write_text(json.dumps(receipt))
    paired_artifacts._validate_receipt(receipt, db)
    pairs = paired_quality.load_pairs(manifest)
    before = db.read_bytes()
    judgments = 0

    def judge(packet: dict[str, str]) -> dict[str, str]:
        nonlocal judgments
        judgments += 1
        return {"winner": "tie", "rationale": "Recorded wiring fixture."}

    with pytest.raises(ValueError, match="configured credential"):
        safety.check_artifacts([manifest.parent], ["fixture-only/plus+quote"])
        for main, branch in pairs:
            paired_quality.compare(main, branch, judge)
    assert judgments == 0
    assert db.read_bytes() == before


@pytest.mark.parametrize(
    "credential_name",
    [
        "OPENALEX_API_KEY",
        "COSCIENTIST_MCP_SHARED_SECRET",
        "ANTHROPIC_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ],
)
def test_cli_uses_configured_credentials_without_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential_name: str
) -> None:
    output = tmp_path / "receipt.json"
    output.write_text('{"diagnostic":"fixture-only/plus+quote"}')
    monkeypatch.setenv(credential_name, "fixture-only/plus+quote")
    monkeypatch.setattr(sys, "argv", ["artifact-safety", str(output)])
    with pytest.raises(ValueError, match="configured credential"):
        safety.main()
