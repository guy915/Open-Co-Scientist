from __future__ import annotations

from typing import Any

import pytest
from co_scientist.core.config import settings
from co_scientist.domains.chat.repository import messages as store_messages
from co_scientist.platform.db.models import MessageRow

from tests._client import create_run as _create_run
from tests._client import fake_litellm as _fake_litellm
from tests._client import make_client as _client
from tests._llm_fake_backend import install_completion_backend
from tests._process_mode_helpers import FakeProcessMode


def test_offline_announcement_is_stored_streamed_and_marked_fallback() -> None:
    rid = _started_run_id()

    body = _announce(rid).text

    rows = _start_rows(rid)
    assert [row.sender for row in rows] == ["user", "system"]
    assert rows[0].content == "Start research"
    assert rows[1].content.strip()
    assert rows[1].meta == {"fallback": True}
    assert body.index('"type": "chunk"') < body.index('"type": "done"')
    assert '"fallback": true' in body
    assert '"type": "error"' not in body


def _started_run_id() -> str:
    c = _client()
    return str(_create_run(c, "Investigate ferroptosis in cancer", tier="express").json()["id"])


def _announce(run_id: str, prompt: str = "Start research") -> Any:
    # TestClient vendors a distinct httpx Response type, so this boundary
    # deliberately returns Any.
    return _client().post(f"/api/runs/{run_id}/messages/started", json={"prompt": prompt})


def _start_rows(run_id: str) -> list[MessageRow]:
    return [m for m in store_messages.list_messages(run_id) if m.kind == "start"]


def test_thinking_only_announcement_retries_before_the_fallback(
    monkeypatch: pytest.MonkeyPatch, fake_process_mode: FakeProcessMode
) -> None:
    # Fixed announcement fallback is reserved for retries that also fail, not
    # the first thinking-only reply.
    rid = _started_run_id()
    fake_process_mode.online()
    monkeypatch.setattr(settings, "chat_model_name", "deepseek/deepseek-v4-pro")
    calls: list[dict[str, Any]] = []
    install_completion_backend(
        monkeypatch,
        (
            _fake_litellm(
                [
                    {
                        "content": None,
                        "reasoning_content": ("brainstorming candidates at length..."),
                    }
                ],
                retry_chunks=[
                    {
                        "content": "Research is under way.",
                        "reasoning_content": None,
                    }
                ],
                calls=calls,
            )
        ).acompletion,
    )

    body = _announce(rid).text

    assert len(calls) == 2
    assert calls[1]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert '"fallback": true' not in body
    reply = _start_rows(rid)[1]
    assert reply.content == "Research is under way."
