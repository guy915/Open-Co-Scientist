from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx

if TYPE_CHECKING:
    from evaluations._paired_db import Snapshot

_COUNTER: ContextVar[Callable[[], None] | None] = ContextVar("benchmark_http_counter", default=None)
COUNTER_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "litellm": version("litellm"),
            "httpx": version("httpx"),
        },
        sort_keys=True,
    ).encode()
).hexdigest()


@contextmanager
def count_http_attempts() -> Iterator[None]:
    from evaluations import quality_benchmark

    class HttpBoundedBackend(quality_benchmark.BoundedBackend):
        def reserve(self) -> None:
            from co_scientist.core.exceptions import LLMCallBudgetExceededError

            with self._lock:
                if self.calls >= self.ceiling:
                    raise LLMCallBudgetExceededError(self.calls + 1, self.ceiling)
                self.calls += 1

        async def complete(self, **completion_args: Any) -> Any:
            completion_args.update(num_retries=0, max_retries=0)
            token = _COUNTER.set(self.reserve)
            try:
                return await self.delegate.complete(**completion_args)
            finally:
                _COUNTER.reset(token)

    original_send = httpx.AsyncClient._send_single_request

    async def send(client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
        reserve = _COUNTER.get()
        if reserve is not None:
            reserve()
        return await original_send(client, request)

    # Count SDK replays and redirects before transport, with no hidden connect retries.
    with (
        patch.dict(
            os.environ,
            {"DISABLE_AIOHTTP_TRANSPORT": "True", "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
        ),
        patch.object(httpx.AsyncClient, "_send_single_request", send),
        patch.object(quality_benchmark, "BoundedBackend", HttpBoundedBackend),
    ):
        yield


def _read_counted_snapshot(db: Path, run_id: str, goal_id: str, source_commit: str) -> Snapshot:
    from evaluations._paired_db import read_snapshot

    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE evaluation_runs ADD COLUMN counter_kind TEXT")
        conn.execute("ALTER TABLE evaluation_runs ADD COLUMN request_counter_sha256 TEXT")
        conn.execute(
            "UPDATE evaluation_runs SET counter_kind=?, request_counter_sha256=? WHERE run_id=?",
            ("http_transport_attempts", COUNTER_SHA256, run_id),
        )
        conn.commit()
        # This is the private evaluation copy, never the serving run database.
        if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise ValueError("benchmark snapshot checkpoint is busy")
    snapshot = read_snapshot(db, run_id, goal_id, source_commit)
    return dataclasses.replace(
        snapshot,
        metrics={
            **snapshot.metrics,
            "call_count_basis": "http_transport_attempts",
            "request_counter_sha256": COUNTER_SHA256,
            "recorded_backend_invocations": None,
        },
    )


@contextmanager
def mark_collection_receipts() -> Iterator[None]:
    from evaluations import quality_benchmark

    with patch.object(quality_benchmark, "read_snapshot", _read_counted_snapshot):
        yield


def main() -> int:
    # An external launcher instruments older clean research refs without editing them.
    sys.path.insert(0, str(Path.cwd()))
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    operation, *args = sys.argv[1:]
    if operation not in {"collect", "compare"}:
        raise ValueError("operation must be collect or compare")
    sys.argv = [sys.argv[0], *args]
    if operation == "collect":
        from evaluations import quality_benchmark

        if "--live" not in args:
            return quality_benchmark.main()
        with count_http_attempts(), mark_collection_receipts():
            return quality_benchmark.main()
    from evaluations import paired_quality

    if "--live-judge" not in args:
        return paired_quality.main()
    with count_http_attempts():
        return paired_quality.main()


if __name__ == "__main__":
    raise SystemExit(main())
