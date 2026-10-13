"""Local-only measurement adapter; the production image/source stays intact."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import threading
import time
from collections import Counter, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi import FastAPI

if (
    os.environ.get("LOAD_TEST") != "1"
    or os.environ.get("COSCIENTIST_DB_PATH") != "/app/data/load.db"
):
    raise RuntimeError("Only the isolated load-test volume is allowed")

_lock = threading.Lock()
_samples: dict[str, deque[float]] = {}
_counts: Counter[str] = Counter()
_original_connect = sqlite3.connect


def record(name: str, milliseconds: float) -> None:
    with _lock:
        _samples.setdefault(name, deque(maxlen=100_000)).append(milliseconds)
        _counts[name] += 1


class MeasuredConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        start = time.perf_counter()
        try:
            return super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                record("sqlite_busy_errors", 0)
            raise
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            record("sqlite_statement_ms", elapsed)
            if sql.lstrip().upper().startswith("BEGIN IMMEDIATE"):
                record("sqlite_writer_acquire_ms", elapsed)


def measured_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    kwargs["factory"] = MeasuredConnection
    return cast(sqlite3.Connection, _original_connect(*args, **kwargs))


sqlite3.connect = measured_connect  # type: ignore[assignment]

from co_scientist.main import app  # noqa: E402
from co_scientist.platform.llm.offline.llm import offline_acompletion  # noqa: E402
from co_scientist.platform.llm.process_mode import install  # noqa: E402
from co_scientist.platform.llm.request.backend import install_backend  # noqa: E402


class FundedTestMode:
    # Real admission remains active; only completion I/O is replaced.
    def is_offline(self) -> bool:
        return False

    def credential_available(self, model: str) -> bool:
        return True


def concrete_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [concrete_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    for union in ("oneOf", "anyOf"):
        if union in value:
            choice: dict[str, Any] = next(
                (item for item in value[union] if item.get("type") != "null"), {}
            )
            return concrete_schema(
                {**{key: item for key, item in value.items() if key != union}, **choice}
            )
    return {key: concrete_schema(item) for key, item in value.items()}


class DeterministicBackend:
    async def complete(self, **args: Any) -> Any:
        await asyncio.sleep(float(os.getenv("LOAD_MODEL_DELAY", "0.1")))
        response = await offline_acompletion(**concrete_schema(args))
        response.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=100, total_tokens=200)
        schema = (args.get("response_format") or {}).get("json_schema", {}).get("schema", {})
        if "offensive_score" in schema.get("properties", {}):
            import json

            payload = json.loads(response.choices[0].message.content)
            payload.update(
                category="allowed",
                offensive_score=1,
                risk_domains=[],
                is_personal_medical_recommendation=False,
                is_personal_finance_recommendation=False,
            )
            response.choices[0].message.content = json.dumps(payload)
        if not args.get("stream"):
            return response

        async def chunks() -> AsyncIterator[Any]:
            text = str(response.choices[0].message.content)
            for offset in range(0, len(text), 80):
                await asyncio.sleep(0.01)
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(delta=SimpleNamespace(content=text[offset : offset + 80]))
                    ],
                    usage=None,
                )
            yield SimpleNamespace(choices=[], usage=response.usage)

        return chunks()

    def supports_json_schema(self, model: str) -> bool:
        return True


install(FundedTestMode())
install_backend(DeterministicBackend())
_original_lifespan = app.router.lifespan_context
_viewers: deque[dict[str, str]] = deque()
_fixture: dict[str, Any] = {}


def seed_fixtures() -> None:
    from co_scientist.domains.chat.repository.examples import open_example_chat
    from co_scientist.orchestration.repository.events import append_event
    from co_scientist.platform import db
    from co_scientist.platform.db import runs
    from co_scientist.platform.db.models import DEMO_CLIENT_ID
    from co_scientist.platform.db.runs import RunCreateOptions

    demo = runs.list_runs(client_id=DEMO_CLIENT_ID)[0]
    chat = open_example_chat(demo.id, "load-report")
    report = runs.list_runs(client_id="load-report")[0]
    _fixture.update(
        report=report.id,
        interview=chat["id"],
        demo=demo.id,
        peers=[f"172.29.251.{101 + index}" for index in range(int(os.getenv("LOAD_PEERS", "40")))],
    )
    for index in range(int(os.getenv("LOAD_FIXTURE_PAGES", "200"))):
        owner = f"load-viewer-{index}"
        run = runs.create_run(
            "Investigate low-energy water purification",
            "express",
            "engine",
            {},
            RunCreateOptions(client_id=owner, llm_backend="offline"),
        )
        # Draft fixtures hold SSE without consuming research-run admission;
        # production draft idle closure and reconnect behavior remain active.
        with db.transaction() as conn:
            append_event(run.id, "progress", {"message": "Waiting for research"}, conn=conn)
        _viewers.append({"owner": owner, "run": run.id})
    # Fixture construction must not consume measured admission counters.
    with db.transaction() as conn:
        conn.execute("DELETE FROM input_admissions")


async def measure_lag() -> None:
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(0.1)
        record("event_loop_lag_ms", max(0, loop.time() - start - 0.1) * 1000)


@asynccontextmanager
async def load_lifespan(application: FastAPI) -> AsyncIterator[None]:
    async with _original_lifespan(application):
        await asyncio.to_thread(seed_fixtures)
        with _lock:
            _samples.clear()
            _counts.clear()
        task = asyncio.create_task(measure_lag())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app.router.lifespan_context = load_lifespan


def percentiles(values: list[float]) -> dict[str, float]:
    values.sort()
    return (
        {f"p{p}": values[min(len(values) - 1, int(len(values) * p / 100))] for p in (50, 95, 99)}
        if values
        else {}
    )


def started_run_statuses() -> dict[str, int]:
    with contextlib.closing(_original_connect(os.environ["COSCIENTIST_DB_PATH"])) as connection:
        return dict(
            connection.execute(
                "SELECT status, COUNT(*) FROM runs "
                "WHERE client_id LIKE 'load-starter-%' GROUP BY status"
            ).fetchall()
        )


@app.get("/__load__/metrics")
async def metrics() -> dict[str, Any]:
    from co_scientist.api.runs import stream_admission

    with _lock:
        result = {
            name: {"count": _counts[name], **percentiles(list(values))}
            for name, values in _samples.items()
        }
    status = Path("/proc/self/status").read_text()
    memory = {
        line.split(":")[0]: int(line.split()[1])
        for line in status.splitlines()
        if line.startswith(("VmRSS:", "VmHWM:"))
    }
    return {
        "timings": result,
        "memory_kib": memory,
        "fds": len(list(Path("/proc/self/fd").iterdir())),
        "active_sse": stream_admission._active,
        "started_run_statuses": await asyncio.to_thread(started_run_statuses),
    }


@app.get("/__load__/fixture")
async def fixture() -> dict[str, Any]:
    return _fixture


@app.get("/__load__/viewer")
async def viewer() -> dict[str, str]:
    return _viewers.popleft()
