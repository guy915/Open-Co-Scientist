from __future__ import annotations

import asyncio
import logging
import time
import types
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, TypeVar

from fastapi.testclient import TestClient
from httpx2 import Response

_T = TypeVar("_T")

# A fixed identity supports restarts within isolated test databases; anonymous
# requests have no private scope.
DEFAULT_TEST_CLIENT_ID = "pytest-default-client"
_DEFAULT_HEADERS = {"X-Client-ID": DEFAULT_TEST_CLIENT_ID}


def drain(gen: AsyncIterator[_T]) -> list[_T]:

    async def _run() -> list[_T]:
        return [item async for item in gen]

    return asyncio.run(_run())


def make_client() -> TestClient:
    from co_scientist.main import app

    return TestClient(app, headers=_DEFAULT_HEADERS)


def create_run(
    client: TestClient,
    goal: str,
    *,
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> Response:
    return client.post("/api/runs", headers=headers, json={"research_goal": goal, **fields})


def make_operator_client() -> TestClient:
    from co_scientist.core.config import settings
    from co_scientist.main import app

    return TestClient(
        app,
        client=("127.0.0.1", 50000),
        headers={**_DEFAULT_HEADERS, "X-Logs-Token": settings.logs_admin_token or ""},
    )


def append_log_row(db_path: str, message: str, **fields: Any) -> int:
    from co_scientist.platform.db import logs
    from co_scientist.platform.db.logs import NewLogRecord

    record: dict[str, Any] = {
        "level": "INFO",
        "levelno": logging.INFO,
        "logger_name": "app.seeded",
        "message": message,
    }
    record.update(fields)
    return logs.append_log(NewLogRecord(**record), db_path=db_path)


def wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_for_status(
    client: TestClient,
    run_id: str,
    status: str,
    *,
    timeout: float = 15.0,
    interval: float = 0.05,
) -> bool:
    # Poll with the owner identity; other identities receive 404.

    def _reached() -> bool:
        response = client.get(f"/api/runs/{run_id}")
        return response.status_code == 200 and bool(response.json().get("status") == status)

    return wait_for(_reached, timeout=timeout, interval=interval)


def start_and_complete(
    client: TestClient,
    goal: str,
    *,
    tier: str = "express",
    timeout: float = 30.0,
) -> str:
    response = create_run(client, goal, tier=tier)
    run_id: str = response.json()["id"]
    assert client.post(f"/api/runs/{run_id}/start", json={}).status_code == 200
    assert wait_for_status(client, run_id, "completed", timeout=timeout), (
        "run did not complete in time"
    )
    return run_id


_StreamChunk = str | Mapping[str, str | None]


def fake_litellm(
    chunks: Sequence[_StreamChunk],
    *,
    raise_exc: Exception | None = None,
    retry_chunks: Sequence[_StreamChunk] | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> types.SimpleNamespace:
    attempts = 0

    async def _chunk_stream(
        response: Sequence[_StreamChunk],
    ) -> AsyncIterator[Any]:
        for chunk in response:
            delta = (
                SimpleNamespace(content=chunk)
                if isinstance(chunk, str)
                else SimpleNamespace(**chunk)
            )
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    async def _acompletion(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal attempts
        attempts += 1
        if calls is not None:
            calls.append(kwargs)
        if raise_exc is not None:
            raise raise_exc
        response = retry_chunks if attempts > 1 and retry_chunks is not None else chunks
        return _chunk_stream(response)

    return types.SimpleNamespace(acompletion=_acompletion)
