from __future__ import annotations

import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from co_scientist.core import byok_scope
from co_scientist.core.config import settings
from co_scientist.domains.access import credentials
from co_scientist.domains.documents import repository as documents
from co_scientist.orchestration.repository import receipts as store_receipts
from co_scientist.platform import db

from tests._client import make_client

_IDEMPOTENCY_OWNER = "idempotency-owner"


_IDEMPOTENCY_KEY = "run-create-01"


_IDEMPOTENCY_PAYLOAD = {
    "research_goal": "Study a defined signaling pathway",
    "tier": "express",
}


def _idempotency_post_run(
    client: Any,
    *,
    owner: str = _IDEMPOTENCY_OWNER,
    request_key: str | None = _IDEMPOTENCY_KEY,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {"X-Client-ID": owner}
    if request_key is not None:
        headers["Idempotency-Key"] = request_key
    headers.update(extra_headers or {})
    return client.post("/api/runs", json=payload or _IDEMPOTENCY_PAYLOAD, headers=headers)


def _owned_run_ids(client: Any, owner: str = _IDEMPOTENCY_OWNER) -> list[str]:
    response = client.get("/api/runs", headers={"X-Client-ID": owner})
    assert response.status_code == 200, response.text
    return [run["id"] for run in response.json()["runs"]]


def test_idempotency_key_replays_conflicts_and_is_scoped_to_its_owner() -> None:
    client = make_client()
    first = _idempotency_post_run(client)
    retry = _idempotency_post_run(client)
    changed = _idempotency_post_run(
        client,
        payload={**_IDEMPOTENCY_PAYLOAD, "research_goal": "Another pathway"},
    )
    other_owner = _idempotency_post_run(client, owner="owner-two")

    assert first.status_code == retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]
    assert changed.status_code == 409
    assert other_owner.status_code == 200
    assert other_owner.json()["id"] != first.json()["id"]
    assert _owned_run_ids(client) == [first.json()["id"]]
    assert _owned_run_ids(client, "owner-two") == [other_owner.json()["id"]]


def test_concurrent_exact_retries_create_one_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    admitted_together = Barrier(2)
    lookup = store_receipts.lookup_run_creation_receipt

    def synchronize_admission(
        owner: str,
        key: str,
        *,
        db_path: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> store_receipts.RunCreationReceipt | None:
        receipt = lookup(owner, key, db_path=db_path, conn=conn)
        # Synchronize after both unlocked lookups, before either writer commits.
        if conn is None:
            admitted_together.wait(timeout=5)
        return receipt

    monkeypatch.setattr(store_receipts, "lookup_run_creation_receipt", synchronize_admission)

    def submit() -> Any:
        return _idempotency_post_run(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [
            future.result(timeout=10) for future in (pool.submit(submit), pool.submit(submit))
        ]

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    run_ids = {response.json()["id"] for response in responses}
    assert len(run_ids) == 1
    assert _owned_run_ids(client) == list(run_ids)


@pytest.mark.parametrize("request_key", ["contains spaces", "bad/key", "x" * 129])
def test_malformed_idempotency_key_is_rejected(request_key: str) -> None:
    client = make_client()
    response = _idempotency_post_run(client, request_key=request_key)

    assert response.status_code == 400
    assert _owned_run_ids(client) == []


def test_changed_byok_key_conflicts_without_echoing_either_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "byok_encryption_key", "test-encryption-secret")
    validated: list[str] = []

    async def accept_credential(
        credential: byok_scope.ByokCredential,
    ) -> None:
        validated.append(credential.api_key)
        return None

    async def no_model_call(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(credentials, "validate_byok_credential", accept_credential)
    monkeypatch.setattr("co_scientist.api.runs.crud.generate_run_title", no_model_call)
    monkeypatch.setattr("co_scientist.api.runs.crud.generate_goal_restatement", no_model_call)

    client = make_client()
    first_secret = "sk-first-private-value"
    changed_secret = "sk-changed-private-value"
    byok_headers = {"X-LLM-Provider": "deepseek"}
    first = _idempotency_post_run(
        client,
        extra_headers={**byok_headers, "X-LLM-API-Key": first_secret},
    )
    changed = _idempotency_post_run(
        client,
        extra_headers={**byok_headers, "X-LLM-API-Key": changed_secret},
    )

    assert first.status_code == 200
    assert changed.status_code == 409
    assert first_secret not in changed.text
    assert changed_secret not in changed.text
    assert _owned_run_ids(client) == [first.json()["id"]]
    assert validated == [first_secret]


def test_concurrent_changed_payloads_commit_one_same_key_run(
    isolated_db: str,
) -> None:
    client = make_client()
    owner = "changed-race-owner"
    key = "changed-payload-race"
    payloads = (
        _IDEMPOTENCY_PAYLOAD,
        {**_IDEMPOTENCY_PAYLOAD, "research_goal": "Study a different pathway"},
    )
    start_together = Barrier(len(payloads))

    def submit(payload: dict[str, Any]) -> Any:
        start_together.wait(timeout=5)
        return _idempotency_post_run(client, owner=owner, request_key=key, payload=payload)

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        futures = [pool.submit(submit, payload) for payload in payloads]
        responses = [future.result(timeout=10) for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 409]
    accepted = next(response for response in responses if response.status_code == 200)
    listed = client.get("/api/runs", headers={"X-Client-ID": owner})
    assert listed.status_code == 200, listed.text
    assert [run["id"] for run in listed.json()["runs"]] == [accepted.json()["id"]]

    with sqlite3.connect(isolated_db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM runs WHERE client_id=?", (owner,)).fetchone()[0] == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_creation_receipts "
                "WHERE client_id=? AND idempotency_key=?",
                (owner, key),
            ).fetchone()[0]
            == 1
        )


_ROLLBACK_OWNER = "run-rollback-owner"


_ROLLBACK_KEY = "rollback-run-01"


def test_late_setup_failure_rolls_back_every_effect_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "byok_encryption_key", "rollback-test-secret")

    async def accept_credential(
        _credential: byok_scope.ByokCredential,
    ) -> None:
        return None

    async def no_model_call(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(credentials, "validate_byok_credential", accept_credential)
    monkeypatch.setattr("co_scientist.api.runs.crud.generate_run_title", no_model_call)
    monkeypatch.setattr("co_scientist.api.runs.crud.generate_goal_restatement", no_model_call)
    client = make_client()
    staged = client.post(
        "/api/documents",
        headers={"X-Client-ID": _ROLLBACK_OWNER},
        files={"file": ("notes.txt", io.BytesIO(b"Pathway notes"), "text/plain")},
        data={"consent": "true"},
    )
    assert staged.status_code == 200, staged.text
    document_id = staged.json()["id"]
    payload: dict[str, Any] = {
        "research_goal": "Study this pathway",
        "tier": "express",
        "document_ids": [document_id],
    }
    provider_key = "sk-rollback-private-value"
    headers = {
        "X-Client-ID": _ROLLBACK_OWNER,
        "Idempotency-Key": _ROLLBACK_KEY,
        "X-LLM-Provider": "deepseek",
        "X-LLM-API-Key": provider_key,
    }

    add_receipt = store_receipts.add_run_creation_receipt

    def fail_after_receipt(*args: Any, **kwargs: Any) -> None:
        add_receipt(*args, **kwargs)
        raise RuntimeError("injected late setup failure")

    with monkeypatch.context() as patch:
        patch.setattr(store_receipts, "add_run_creation_receipt", fail_after_receipt)
        with pytest.raises(RuntimeError, match="injected late setup failure"):
            client.post("/api/runs", headers=headers, json=payload)

    assert client.get("/api/runs", headers={"X-Client-ID": _ROLLBACK_OWNER}).json()["runs"] == []
    document = documents.get_staged_documents([document_id], _ROLLBACK_OWNER)[0]
    assert document["run_id"] is None
    with db.connect() as conn:
        for table in (
            "runs",
            "run_events",
            "evidence",
            "run_credentials",
            "run_creation_receipts",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    retried = client.post("/api/runs", headers=headers, json=payload)
    assert retried.status_code == 200, retried.text
    assert (
        documents.get_staged_documents([document_id], _ROLLBACK_OWNER)[0]["run_id"]
        == (retried.json()["id"])
    )
    with db.connect() as conn:
        digest = conn.execute(
            "SELECT request_digest FROM run_creation_receipts WHERE run_id=?",
            (retried.json()["id"],),
        ).fetchone()[0]
    assert provider_key not in digest
