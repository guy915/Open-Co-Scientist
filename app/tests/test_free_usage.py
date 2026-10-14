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


@pytest.mark.parametrize("overrides", [{"tier": "standard"}, {"max_iterations": 1}])
def test_free_run_must_be_express_without_numeric_overrides(
    real_backend: None, overrides: dict[str, Any]
) -> None:
    with TestClient(app) as client:
        response = _create(client, **overrides)
        assert response.status_code == 403
        assert client.get("/api/runs", headers=_CLIENT).json()["runs"] == []


def test_free_runs_are_capped_per_day(real_backend: None) -> None:
    with TestClient(app) as client:
        for _ in range(3):
            assert _create(client).status_code == 200
        response = _create(client)
        assert response.status_code == 429
        assert "3 free runs" in response.json()["detail"]
        runs = client.get("/api/runs", headers=_CLIENT).json()["runs"]
        assert len(runs) == 3
        other = _create(client, headers={"X-Client-ID": "another-device"})
        assert other.status_code == 200


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
