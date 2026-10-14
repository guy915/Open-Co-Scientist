from __future__ import annotations

import asyncio

import pytest
from co_scientist.core import run_modes
from co_scientist.domains.chat import seed
from co_scientist.orchestration.engine_adapter.opts import _generator_kwargs
from co_scientist.platform.db import logs, runs
from co_scientist.platform.db.models import DEMO_CLIENT_ID, RunStatus
from co_scientist.science.scheduling import (
    Budget,
    SchedulerStats,
    TaskType,
    TerminationReason,
)
from co_scientist.science.scheduling.policy import (
    decide_next_task,
)
from fastapi.testclient import TestClient

from tests._client import append_log_row, make_client, wait_for
from tests._client import create_run as _create_run

_DELETION_OWNER = {"X-Client-ID": "delete-owner"}
_DELETION_OTHER = {"X-Client-ID": "someone-else"}


def _wait_owned_status(
    client: TestClient, run_id: str, status: str, *, timeout: float = 30.0
) -> bool:
    # Poll with the explicit owner identity; a default client cannot read
    # another identity's run.

    def _reached() -> bool:
        response = client.get(f"/api/runs/{run_id}", headers=_DELETION_OWNER)
        return response.status_code == 200 and bool(response.json().get("status") == status)

    return wait_for(_reached, timeout=timeout)


def _run_to_completion(client: TestClient, goal: str) -> str:
    created = _create_run(client, goal, headers=_DELETION_OWNER, tier="express")
    assert created.status_code == 200, created.text
    run_id: str = created.json()["id"]
    started = client.post(f"/api/runs/{run_id}/start", headers=_DELETION_OWNER, json={})
    assert started.status_code == 200, started.text
    assert _wait_owned_status(client, run_id, "completed")
    return run_id


def test_delete_cascades_across_every_run_scoped_table(
    isolated_db: str,
) -> None:
    client = make_client()
    run_id = _run_to_completion(client, "Cascade delete goal")

    before = runs.count_run_rows(run_id, db_path=isolated_db)
    assert before["runs"] == 1
    assert before["hypotheses"] > 0
    assert before["reports"] > 0
    nonzero_tables = [t for t, n in before.items() if n > 0]
    assert len(nonzero_tables) >= 4

    response = client.delete(f"/api/runs/{run_id}", headers=_DELETION_OWNER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["counts"] == before

    after = runs.count_run_rows(run_id, db_path=isolated_db)
    assert all(count == 0 for count in after.values()), after
    assert not runs.run_exists(run_id)
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_delete_removes_the_runs_persisted_log_rows(
    isolated_db: str,
) -> None:
    # Logs have no run foreign key; explicit deletion must scrub research goals
    # beyond cascading tables.
    client = make_client()
    created = _create_run(client, "deletion cascade probe", headers=_DELETION_OWNER)
    run_id = created.json()["id"]

    append_log_row(
        isolated_db,
        "Supervisor analyzing research goal: deletion cascade probe",
        run_id=run_id,
    )
    append_log_row(
        isolated_db,
        "report research_goal=deletion cascade probe run_mode=standard",
        run_id=run_id,
    )
    other_run = _create_run(client, "a different tenant's goal", headers=_DELETION_OTHER).json()[
        "id"
    ]
    other_row_id = append_log_row(isolated_db, "other tenant's line", run_id=other_run)
    app_wide_row_id = append_log_row(isolated_db, "app-wide line")

    assert logs.count_logs_for_run(run_id, db_path=isolated_db) == 2

    response = client.delete(f"/api/runs/{run_id}", headers=_DELETION_OWNER)
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["app_logs"] == 2

    assert logs.count_logs_for_run(run_id, db_path=isolated_db) == 0
    remaining = logs.list_logs(db_path=isolated_db)
    assert "deletion cascade probe" not in " ".join(row["message"] for row in remaining)
    remaining_ids = {row["id"] for row in remaining}
    assert other_row_id in remaining_ids
    assert app_wide_row_id in remaining_ids


_TIERS = ("express", "standard", "extended", "ultra")


def _tier_budget(tier: str) -> Budget:
    cfg = run_modes.resolved_run_config({"tier": tier})
    kwargs = _generator_kwargs(cfg, "offline/test", None)
    return Budget(
        max_iterations=int(kwargs["max_iterations"]),
        **kwargs["options"].budget,
    )


def _steady_state_coverage(cfg: dict[str, int]) -> float:
    return 2.0 * cfg["tournament_pairs"] * cfg["max_iterations"] / (cfg["initial_hypotheses_count"])


def _worked_stats(cfg: dict[str, int]) -> SchedulerStats:
    pool = cfg["initial_hypotheses_count"] + (cfg["evolution_max_count"] * cfg["max_iterations"])
    return SchedulerStats(
        pool_size=pool,
        reviewed_count=pool,
        unreviewed_count=0,
        rankable_count=cfg["initial_hypotheses_count"],
        match_coverage=_steady_state_coverage(cfg),
        total_matches=2 * cfg["tournament_pairs"] * cfg["max_iterations"],
        iteration=0,
    )


@pytest.mark.parametrize("tier", _TIERS)
def test_tier_ceilings_sit_above_steady_state_and_spare_the_first_tournament(
    tier: str,
) -> None:
    # Ceilings fire on equality; values at configured steady state stop runs
    # prematurely.
    cfg = run_modes.RUN_TIER_DEFAULTS[tier]
    budget = _tier_budget(tier)
    assert budget.max_matches_per_idea is not None
    assert budget.max_matches_per_idea > _steady_state_coverage(cfg)
    assert budget.max_ideas is not None
    assert budget.max_ideas > cfg["initial_hypotheses_count"] + (
        cfg["evolution_max_count"] * cfg["max_iterations"]
    )

    decision = decide_next_task(_worked_stats(cfg), budget)

    assert decision.termination_reason not in {
        TerminationReason.MAX_MATCHES_PER_IDEA,
        TerminationReason.MAX_IDEAS,
    }
    assert decision.next_task is not TaskType.TERMINATE


_RENAME_OWNER = {"X-Client-ID": "rename-owner"}

_TITLE = "Sequential Senolytic Conditioning for Cryogenic Biostasis"


def _draft_run(client: TestClient, goal: str = "Extend healthy lifespan") -> str:
    created = _create_run(client, goal, headers=_RENAME_OWNER, tier="express")
    assert created.status_code == 200, created.text
    return str(created.json()["id"])


def test_renaming_a_run_persists_the_cleaned_title() -> None:
    client = make_client()
    run_id = _draft_run(client)

    body = client.patch(
        f"/api/runs/{run_id}",
        headers=_RENAME_OWNER,
        json={"title": f"  {_TITLE}\n"},
    ).json()

    assert body["title"] == _TITLE
    assert body["research_goal"] == "Extend healthy lifespan"
    fetched = client.get(f"/api/runs/{run_id}", headers=_RENAME_OWNER).json()
    assert fetched["title"] == _TITLE
    listed = client.get("/api/runs", headers=_RENAME_OWNER).json()["runs"]
    assert [r["title"] for r in listed if r["id"] == run_id] == [_TITLE]


def test_rename_and_delete_refuse_foreign_demo_and_unknown_runs(isolated_db: str) -> None:
    client = make_client()
    run_id = _draft_run(client)
    stranger = {"X-Client-ID": "someone-else"}
    asyncio.run(seed.seed_demo_runs(isolated_db))
    demo = runs.list_runs(client_id=DEMO_CLIENT_ID, db_path=isolated_db)[0]

    def refused(method: str, run: str, headers: dict[str, str]) -> int:
        return client.request(
            method, f"/api/runs/{run}", headers=headers, json={"title": _TITLE}
        ).status_code

    assert refused("PATCH", run_id, stranger) == 404
    assert refused("DELETE", run_id, stranger) == 404
    assert refused("PATCH", "no-such-run", _RENAME_OWNER) == 404
    assert refused("DELETE", "no-such-run", _RENAME_OWNER) == 404
    assert refused("PATCH", demo.id, _RENAME_OWNER) == 403
    assert refused("DELETE", demo.id, _RENAME_OWNER) == 403
    runs.update_run_status(run_id, RunStatus.RUNNING)
    assert refused("DELETE", run_id, _RENAME_OWNER) == 409
    run = runs.get_run(run_id)
    assert run is not None and run.title != _TITLE
