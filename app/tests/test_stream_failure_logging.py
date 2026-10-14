from __future__ import annotations

import io
import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
from co_scientist.core import byok_scope
from co_scientist.domains.chat import qa, run_start_announcement
from co_scientist.domains.chat.repository import messages
from co_scientist.domains.chat.repository.messages import NewMessage
from co_scientist.platform.db import logs
from co_scientist.platform.db.log_capture import configure_log_capture, shutdown_log_capture
from co_scientist.platform.telemetry.logging_setup import configure_logging

from tests._llm_fake_backend import install_completion_backend
from tests._process_mode_helpers import FakeProcessMode
from tests._store_helpers import seed_run


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["qa", "announcement"])
@pytest.mark.parametrize("partial", [False, True])
async def test_stream_failures_never_log_worker_or_supervisor_keys(
    monkeypatch: pytest.MonkeyPatch,
    fake_process_mode: FakeProcessMode,
    operation: str,
    partial: bool,
) -> None:
    worker_key = "synthetic-worker-failure-key"
    supervisor_key = "synthetic-supervisor-failure-key"
    credential = byok_scope.ByokCredential(
        provider="deepseek",
        api_key=worker_key,
        model="deepseek/deepseek-v4-flash",
        supervisor_provider="openai",
        supervisor_api_key=supervisor_key,
        supervisor_model="openai/gpt-4o",
    )
    run = seed_run("Study a synthetic goal", client_id="owner", profile="express")
    question = messages.append_message(
        NewMessage(run_id=run.id, sender="user", content="What is next?", kind="qa")
    )
    fake_process_mode.online()

    async def chunks() -> AsyncGenerator[SimpleNamespace, None]:
        if partial:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="Safe prose", tool_calls=[]))
                ]
            )
        raise RuntimeError(f"provider echoed {worker_key} and {supervisor_key}")

    async def respond(**kwargs: Any) -> AsyncGenerator[SimpleNamespace, None]:
        return chunks()

    install_completion_backend(monkeypatch, respond)
    stream = io.StringIO()
    handler = configure_logging()
    handler.stream = stream  # type: ignore[attr-defined]
    configure_log_capture()
    try:
        if operation == "qa":
            frames = [
                frame
                async for frame in qa.stream_answer(
                    run.id,
                    qa.QaQuestion(question.content, question.id),
                    qa.QaAnswerInputs("Use supplied evidence", []),
                    credential,
                )
            ]
        else:
            frames = [
                frame
                async for frame in run_start_announcement.stream_announcement(
                    run, question.id, credential
                )
            ]
        shutdown_log_capture()
        persisted = logs.list_logs(limit=1000)
    finally:
        shutdown_log_capture()
        configure_logging()

    captured = stream.getvalue() + json.dumps(persisted)
    assert worker_key not in captured
    assert supervisor_key not in captured
    assert worker_key not in "".join(frames)
    assert supervisor_key not in "".join(frames)
    assert byok_scope.current_byok() is None
    assert frames
    failure_records = [
        row
        for row in persisted
        if row["logger"]
        in {"co_scientist.domains.chat.qa", "co_scientist.domains.chat.run_start_announcement"}
    ]
    assert failure_records
    assert all(row["run_id"] == run.id for row in failure_records)
