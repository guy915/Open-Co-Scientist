from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from co_scientist.api.tracing import TracingMiddleware
from co_scientist.orchestration import engine_tasks, task_worker
from co_scientist.platform.db.models import ScientificTask
from co_scientist.platform.telemetry import tracing
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests._store_helpers import enqueue_task, seed_run


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_provider", provider)
    yield exporter
    provider.shutdown()


def _only(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    (span,) = [s for s in exporter.get_finished_spans() if s.name == name]
    return span


@pytest.mark.asyncio
async def test_a_task_span_parents_its_work_and_records_the_commit(
    isolated_db: str, monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    run = seed_run("traced goal")
    task = enqueue_task(run.id, "engine.node.generate", "traced", db_path=isolated_db)

    async def _execute(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        with tracing.current_span("inner", {}):
            return {"ok": True}

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _execute)
    assert await task_worker.run_once("worker-a", db_path=isolated_db)

    outer = _only(spans, "task.execute")
    inner = _only(spans, "inner")
    assert inner.parent is not None and outer.context is not None
    assert inner.parent.span_id == outer.context.span_id
    assert outer.attributes == {
        "co_scientist.run_id": run.id,
        "co_scientist.task.id": task.id,
        "co_scientist.task.type": "engine.node.generate",
        "co_scientist.task.node": "generate",
        "co_scientist.task.attempt": 1,
        "co_scientist.task.outcome": "committed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "max_attempts", "outcome"),
    [
        (RuntimeError("transient"), 3, "retried"),
        (RuntimeError("terminal"), 1, "failed"),
        (engine_tasks.SupersededTaskError("advanced"), 3, "superseded"),
    ],
)
async def test_a_task_span_names_how_a_failure_settled(
    isolated_db: str,
    monkeypatch: pytest.MonkeyPatch,
    spans: InMemorySpanExporter,
    error: Exception,
    max_attempts: int,
    outcome: str,
) -> None:
    run = seed_run("traced failure")
    enqueue_task(run.id, "engine.test.fail", "fail", max_attempts=max_attempts, db_path=isolated_db)

    async def _execute(_task: ScientificTask, *, db_path: str | None = None) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(engine_tasks, "execute_engine_task", _execute)
    assert await task_worker.run_once("worker-a", db_path=isolated_db)

    assert (_only(spans, "task.execute").attributes or {})["co_scientist.task.outcome"] == outcome


def _traced_app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret detail")

    app.add_middleware(TracingMiddleware)
    return app


def test_a_request_span_is_named_by_its_route_template(spans: InMemorySpanExporter) -> None:
    client = TestClient(_traced_app())

    assert client.get("/items/42?q=private").status_code == 200

    span = _only(spans, "GET /items/{item_id}")
    assert span.kind is trace.SpanKind.SERVER
    assert span.attributes == {
        "http.request.method": "GET",
        "http.response.status_code": 200,
        "http.route": "/items/{item_id}",
    }


def test_a_failing_request_records_the_error_type_only(spans: InMemorySpanExporter) -> None:
    client = TestClient(_traced_app(), raise_server_exceptions=False)

    assert client.get("/boom").status_code == 500

    span = _only(spans, "GET /boom")
    assert span.status.status_code is trace.StatusCode.ERROR
    assert (span.attributes or {})["error.type"] == "RuntimeError"
    assert not span.events
    assert "secret" not in (span.status.description or "")


def test_the_api_wraps_every_middleware_in_the_request_span() -> None:
    from co_scientist.main import app

    assert cast(object, app.user_middleware[0].cls) is TracingMiddleware
