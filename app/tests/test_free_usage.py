from __future__ import annotations

from typing import Any

import pytest
from co_scientist.core.config import settings
from co_scientist.main import app
from fastapi.testclient import TestClient

from tests._process_mode_helpers import FakeProcessMode

_CLIENT = {"X-Client-ID": "free-usage-scientist"}


@pytest.fixture
def real_backend(monkeypatch: pytest.MonkeyPatch, fake_process_mode: FakeProcessMode) -> None:
    fake_process_mode.online()

    async def _no_title(goal: str) -> None:
        return None

    monkeypatch.setattr("co_scientist.api.runs.crud.generate_run_title", _no_title)
    monkeypatch.setattr("co_scientist.api.runs.crud.generate_goal_restatement", _no_title)


def _create(
    client: TestClient,
    headers: dict[str, str] | None = None,
    **body: Any,
) -> Any:
    return client.post(
        "/api/runs",
        json={"research_goal": "free goal", "tier": "express", **body},
        headers=headers or _CLIENT,
    )


def test_free_run_rejects_numeric_overrides(real_backend: None) -> None:
    with TestClient(app) as client:
        response = _create(client, max_iterations=1)
        assert response.status_code == 403
        assert client.get("/api/runs", headers=_CLIENT).json()["runs"] == []


def test_free_runs_are_capped_per_day(real_backend: None) -> None:
    with TestClient(app) as client:
        for _ in range(4):
            assert _create(client).status_code == 200
        response = _create(client)
        assert response.status_code == 429
        assert "used your free runs" in response.json()["detail"]
        runs = client.get("/api/runs", headers=_CLIENT).json()["runs"]
        assert len(runs) == 4
        other = _create(client, headers={"X-Client-ID": "another-device"})
        assert other.status_code == 200


@pytest.mark.parametrize(
    ("tiers", "refused"),
    [
        (["standard"] * 3, "express"),
        (["extended"] * 2, "express"),
        (["ultra", "standard"], "express"),
        (["ultra"], "extended"),
        (["extended", "standard"], "standard"),
    ],
)
def test_free_credits_weight_each_tier(real_backend: None, tiers: list[str], refused: str) -> None:
    with TestClient(app) as client:
        for tier in tiers:
            assert _create(client, tier=tier).status_code == 200
        response = _create(client, tier=refused)
        assert response.status_code == 429
        usage = client.get("/api/free-usage", headers=_CLIENT).json()
        costs = usage["costs"]
        assert usage["used"] == sum(costs[tier] for tier in tiers)
        assert usage["remaining"] < costs[refused]


def test_one_ultra_leaves_room_for_a_smaller_run(real_backend: None) -> None:
    with TestClient(app) as client:
        assert _create(client, tier="ultra").status_code == 200
        assert _create(client, tier="ultra").status_code == 429
        denied = _create(client, tier="extended")
        assert "needs 6 credits" in denied.json()["detail"]
        assert _create(client, tier="standard").status_code == 200
        assert client.get("/api/free-usage", headers=_CLIENT).json()["remaining"] == 0


@pytest.mark.parametrize(
    ("worker_model", "stamped"),
    [
        ("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", True),
        ("openrouter/inclusionai/ling-3.1-flash", True),
        ("openrouter/z-ai/glm-5.3-flash", False),
    ],
)
def test_only_a_free_run_on_free_routes_is_stamped_zero_cost(
    real_backend: None,
    monkeypatch: pytest.MonkeyPatch,
    worker_model: str,
    stamped: bool,
) -> None:
    from co_scientist.platform.db import runs

    monkeypatch.setattr(settings, "model_name", worker_model)
    with TestClient(app) as client:
        run_id = _create(client).json()["id"]
    run = runs.get_run(run_id)
    assert run is not None
    assert (run.config.get("zero_cost_admission") is True) is stamped
