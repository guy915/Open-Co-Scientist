from __future__ import annotations

import logging
import types
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from co_scientist.core.config import settings
from co_scientist.domains.chat.goal_text import (
    generate_goal_restatement,
    generate_run_title,
)
from co_scientist.domains.research_state.elo import live_leaderboard

from tests._llm_fake_backend import install_completion_backend

_GENERATORS = [
    pytest.param(
        generate_goal_restatement,
        "This investigation seeks the loop's control point.",
        id="restatement",
    ),
    pytest.param(generate_run_title, "Ferroptosis In Glioma", id="title"),
]


def test_leaderboard_ranks_played_ideas_above_unplayed_ones() -> None:
    rows = live_leaderboard(
        [
            {"id": "unplayed", "title": "Never matched", "elo_rating": 1200},
            {
                "id": "loser",
                "title": "Lost a match",
                "elo_rating": 1136,
                "loss_count": 1,
            },
            {
                "id": "winner",
                "title": "Won a match",
                "elo_rating": 1259,
                "win_count": 1,
            },
        ]
    )

    assert [row["id"] for row in rows] == ["winner", "loser", "unplayed"]


def _thinking_only() -> Any:
    message = types.SimpleNamespace(content="")
    details = types.SimpleNamespace(reasoning_tokens=900)
    usage = types.SimpleNamespace(completion_tokens_details=details)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)


@pytest.mark.parametrize(("generate", "answer"), _GENERATORS)
async def test_generation_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
    generate: Callable[[str], Awaitable[str | None]],
    answer: str,
) -> None:
    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    install_completion_backend(monkeypatch, _boom)
    monkeypatch.setattr(settings, "chat_model_name", "deepseek/deepseek-v4-pro")

    assert await generate("Map the feedback loop.") is None


async def test_goal_text_retries_keep_separate_operation_budgets(
    monkeypatch: pytest.MonkeyPatch,
    reachable_provider: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def reasoning_only(**_kwargs: Any) -> Any:
        return _thinking_only()

    fake = install_completion_backend(monkeypatch, reasoning_only)
    monkeypatch.setattr(settings, "chat_model_name", "deepseek/deepseek-v4-pro")
    monkeypatch.setattr(settings, "app_llm_max_calls", 1)
    with caplog.at_level(logging.INFO, logger="co_scientist.platform.llm.llm_scope"):
        assert await generate_run_title("Map the feedback loop.") is None
        assert await generate_goal_restatement("Map the feedback loop.") is None

    assert len(fake.requests) == 2
    assert "surface=title calls=1" in caplog.text
    assert "surface=goal_restatement calls=1" in caplog.text
