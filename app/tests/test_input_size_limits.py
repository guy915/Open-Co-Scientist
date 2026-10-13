from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from co_scientist.core.config import settings
from co_scientist.main import app
from co_scientist.platform import db
from co_scientist.platform.db.models import RunRow
from fastapi.testclient import TestClient

from tests._client import create_run, make_client


def _limit(monkeypatch: pytest.MonkeyPatch, name: str, value: int) -> None:
    if name in type(settings).model_fields:
        monkeypatch.setattr(settings, name, value)
    else:
        monkeypatch.setattr(type(settings), name, value, raising=False)


def test_transport_rejects_large_json_before_parsing() -> None:
    client = make_client()
    payload = json.dumps({"research_goal": "Study folding", "padding": "x" * 1_048_576})
    response = client.post(
        "/api/runs", content=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["research_goal", "requirements"])
def test_planning_fields_reject_oversized_content(field: str) -> None:
    body: dict[str, object] = {"research_goal": "Study folding"}
    body[field] = "x" * 65_536 if field == "research_goal" else ["x" * 65_536]
    response = make_client().post("/api/runs", json=body)
    assert response.status_code == 422
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_steering_rejects_oversized_content() -> None:
    client = make_client()
    run_id = create_run(client, "Study folding").json()["id"]
    response = client.post(f"/api/runs/{run_id}/messages", json={"content": "x" * 65_536})
    assert response.status_code == 422
    assert client.get(f"/api/runs/{run_id}/messages").json()["messages"] == []


def test_repeated_staging_reuses_the_owned_document() -> None:
    client = make_client()
    first = client.post(
        "/api/documents",
        files={"file": ("research.txt", b"x" * 524_288, "text/plain")},
        data={"consent": "true"},
    )
    second = client.post(
        "/api/documents",
        files={"file": ("research.txt", b"x" * 524_288, "text/plain")},
        data={"consent": "true"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM staged_documents").fetchone()[0] == 1


def test_staged_owner_budget_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "staged_document_client_bytes", 1_024)
    client = make_client()
    first = client.post(
        "/api/documents",
        files={"file": ("first.txt", b"a" * 700, "text/plain")},
        data={"consent": "true"},
    )
    denied = client.post(
        "/api/documents",
        files={"file": ("second.txt", b"b" * 700, "text/plain")},
        data={"consent": "true"},
    )
    assert first.status_code == 200
    assert denied.status_code == 429
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM staged_documents").fetchone()[0] == 1


def test_concurrent_staging_cannot_overdraw_global_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "staged_document_global_bytes", 1_024)

    def submit(owner: str) -> int:
        client = TestClient(app, headers={"X-Client-ID": owner})
        response = client.post(
            "/api/documents",
            files={"file": ("research.txt", owner.encode() * 700, "text/plain")},
            data={"consent": "true"},
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, ("a", "b"))) == [200, 429]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM staged_documents").fetchone()[0] == 1


def test_rotating_owners_cannot_reset_host_write_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "anonymous_write_host_requests_per_day", 2)
    client = make_client()
    for owner in ("first", "second"):
        assert (
            create_run(client, "Study folding", headers={"X-Client-ID": owner}).status_code == 200
        )
    response = create_run(client, "Study folding", headers={"X-Client-ID": "third"})
    assert response.status_code == 429
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


def test_attachment_copies_share_owner_storage_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "human_corpus_client_bytes", 2_500)
    client = make_client()
    first = create_run(client, "Study folding").json()["id"]
    second = create_run(client, "Study catalysts").json()["id"]
    body = {"title": "Paper", "text": "a" * 700, "consent": True}
    assert client.post(f"/api/runs/{first}/attachments", json=body).status_code == 200
    assert client.post(f"/api/runs/{second}/attachments", json=body).status_code == 429
    with db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence WHERE source='attachment'").fetchone()[0]
            == 1
        )


def test_user_messages_share_owner_storage_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _limit(monkeypatch, "user_message_client_bytes", 1_024)
    client = make_client()
    first = create_run(client, "Study folding").json()["id"]
    second = create_run(client, "Study catalysts").json()["id"]
    assert (
        client.post(f"/api/runs/{first}/messages", json={"content": "a" * 700}).status_code == 200
    )
    assert (
        client.post(f"/api/runs/{second}/messages", json={"content": "b" * 700}).status_code == 429
    )
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE sender='user'").fetchone()[0] == 1


def test_retention_removes_old_drafts_and_preserves_live_work() -> None:
    import time

    from co_scientist.domains.access.retention import sweep_expired_runs

    client = make_client()
    old = create_run(client, "Abandoned goal").json()["id"]
    live = create_run(client, "Queued goal").json()["id"]
    recent = create_run(client, "Recent goal").json()["id"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE runs SET created_at=?,updated_at=? WHERE id IN (?,?)",
            (time.time() - 14 * 86400, time.time() - 14 * 86400, old, live),
        )
        conn.execute("UPDATE runs SET status='queued' WHERE id=?", (live,))
    assert sweep_expired_runs() == [old]
    with db.connect() as conn:
        assert {r[0] for r in conn.execute("SELECT id FROM runs")} == {live, recent}


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_chunked_and_false_lengths_cannot_bypass_body_limits(declared_length: str | None) -> None:
    import asyncio

    from co_scientist.api.request_limits import RequestLimitsMiddleware
    from starlette.types import Message, Scope

    called = False
    sent: list[Message] = []
    chunks: list[Message] = [
        {"type": "http.request", "body": b"x" * 150_000, "more_body": True},
        {"type": "http.request", "body": b"x" * 150_000, "more_body": False},
    ]
    headers = [(b"x-client-id", b"chunks")]
    if declared_length is not None:
        headers.append((b"content-length", declared_length.encode()))
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/runs",
        "headers": headers,
        "client": ("192.0.2.1", 123),
    }

    async def handler(scope: Scope, receive: object, send: object) -> None:
        nonlocal called
        called = True

    async def receive() -> Message:
        return chunks.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    asyncio.run(RequestLimitsMiddleware(handler)(scope, receive, send))
    assert not called
    assert sent[0]["status"] == 413


def test_draft_retention_rechecks_a_racing_start(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from co_scientist.domains.access import retention
    from co_scientist.platform.db import runs as store_runs

    run_id = create_run(make_client(), "Racing goal").json()["id"]
    with db.connect() as conn:
        conn.execute("UPDATE runs SET updated_at=? WHERE id=?", (time.time() - 14 * 86400, run_id))
    original = store_runs.list_expired_draft_runs

    def racing_lookup(cutoff: float, db_path: str | None = None) -> list[RunRow]:
        rows = original(cutoff, db_path)
        with db.connect(db_path) as conn:
            conn.execute("UPDATE runs SET status='queued' WHERE id=?", (run_id,))
        return rows

    monkeypatch.setattr(store_runs, "list_expired_draft_runs", racing_lookup)
    assert retention.sweep_expired_runs() == []
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] == "queued"
        )


def test_deletion_and_reopening_connections_cannot_refund_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _limit(monkeypatch, "anonymous_write_host_requests_per_day", 1)
    client = make_client()
    run_id = create_run(client, "Study folding").json()["id"]
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    db._initialized.clear()
    assert create_run(client, "Another goal", headers={"X-Client-ID": "rotated"}).status_code == 429


@pytest.mark.parametrize("scope", ["client", "host", "global"])
def test_ingress_byte_admission_counts_utf8_and_survives_reconnection(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    from co_scientist.core.exceptions import StorageAdmissionError
    from co_scientist.platform.db.storage_admission import reserve_write

    _limit(monkeypatch, f"anonymous_write_{scope}_bytes_per_day", 1024)
    reserve_write("owner", "peer", 700)
    db._initialized.clear()
    with pytest.raises(StorageAdmissionError):
        reserve_write("owner" if scope == "client" else "rotated", "peer", 700)
    with db.connect() as conn:
        assert conn.execute(
            "SELECT requests,bytes FROM input_admissions WHERE scope=?", (scope,)
        ).fetchone()[:] == (1, 700)


def test_retention_releases_logical_storage_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    from co_scientist.platform.db.storage_admission import reserve_write

    with db.connect() as conn:
        conn.execute("CREATE TABLE storage_probe (data BLOB)")
        conn.execute("INSERT INTO storage_probe VALUES (zeroblob(1000000))")
        conn.execute("DELETE FROM storage_probe")
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    assert free > 0
    _limit(monkeypatch, "anonymous_store_max_bytes", (pages - free) * page_size + 8192)
    reserve_write("owner", "peer", 512)


def test_example_copy_charges_stored_bytes_before_commit(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from co_scientist.domains.chat import seed
    from co_scientist.platform.db import runs as store_runs
    from co_scientist.platform.db.models import DEMO_CLIENT_ID

    asyncio.run(seed.seed_demo_runs(isolated_db))
    source = store_runs.list_runs(client_id=DEMO_CLIENT_ID)[0]
    _limit(monkeypatch, "anonymous_write_client_bytes_per_day", 1024)
    response = make_client().post(f"/api/runs/{source.id}/example-chat")
    assert response.status_code == 429
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM runs WHERE client_id!=?", (DEMO_CLIENT_ID,)
            ).fetchone()[0]
            == 0
        )
