from __future__ import annotations

import asyncio
import json

import pytest
from co_scientist.domains.chat import seed
from co_scientist.domains.chat.repository import interviews
from co_scientist.domains.chat.repository.examples import open_example_chat
from co_scientist.main import app
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import DEMO_CLIENT_ID, RunRow
from co_scientist.platform.db.runs import RunCreateOptions
from fastapi.testclient import TestClient


def _seed(db_path: str) -> list[RunRow]:
    asyncio.run(seed.seed_demo_runs(db_path))
    return runs.list_runs(client_id=DEMO_CLIENT_ID, db_path=db_path)


def test_reserved_demo_header_cannot_read_or_mutate_as_demo_owner(isolated_db: str) -> None:
    demos = _seed(isolated_db)
    demo = demos[0]
    interview_id = str(demo.config["interview_id"])
    before = interviews.get_interview(interview_id, db_path=isolated_db)
    assert before is not None
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Client-ID": DEMO_CLIENT_ID}

    responses = [
        client.get("/api/runs/demo", headers=headers),
        client.get(f"/api/runs/{demo.id}", headers=headers),
        client.get("/api/interviews", headers=headers),
        client.get(f"/api/interviews/{interview_id}", headers=headers),
        client.delete(f"/api/interviews/{interview_id}", headers=headers),
        client.put(
            f"/api/interviews/{interview_id}/turns/1",
            headers=headers,
            json={"content": "poison"},
        ),
        client.post(f"/api/interviews/{interview_id}/turns/1/retry", headers=headers),
        client.post(
            f"/api/interviews/{interview_id}/turns",
            headers=headers,
            json={"content": "poison"},
        ),
        client.put(
            f"/api/interviews/{interview_id}/fields",
            headers=headers,
            json={
                "research_challenge": "poison",
                "focus_area": [],
                "preferences": [],
                "lab_constraints": [],
            },
        ),
        client.post(
            "/api/runs",
            headers=headers,
            json={"research_goal": "poison"},
        ),
        client.patch(f"/api/runs/{demo.id}", headers=headers, json={"title": "poison"}),
        client.delete(f"/api/runs/{demo.id}", headers=headers),
        client.post(f"/api/runs/{demo.id}/start", headers=headers, json={}),
        client.post(f"/api/runs/{demo.id}/cancel", headers=headers, json={}),
        client.post(f"/api/runs/{demo.id}/messages", headers=headers, json={}),
        client.post(f"/api/runs/{demo.id}/messages/ask", headers=headers, json={}),
        client.post(f"/api/runs/{demo.id}/messages/started", headers=headers, json={}),
        client.post(f"/api/runs/{demo.id}/messages/1/revise", headers=headers, json={}),
        client.post(
            "/api/interviews",
            headers=headers,
            json={"research_challenge": "poison"},
        ),
        client.post(f"/api/runs/{demo.id}/example-chat", headers=headers),
    ]

    assert [response.status_code for response in responses] == [400] * len(responses)
    assert all(response.json()["detail"] for response in responses)
    assert len(client.get("/api/runs/demo").json()["runs"]) == len(demos)
    assert client.get(f"/api/runs/{demo.id}").status_code == 200
    assert interviews.get_interview(interview_id, db_path=isolated_db) == before
    client.close()


def test_reserved_demo_owner_is_rejected_by_store_creation(isolated_db: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        runs.create_run(
            "not a seed",
            "standard",
            "openai",
            {},
            RunCreateOptions(client_id=DEMO_CLIENT_ID, db_path=isolated_db),
        )
    with pytest.raises(ValueError, match="reserved"):
        interviews.create_interview(DEMO_CLIENT_ID, "not a seed", db_path=isolated_db)
    assert runs.list_runs(client_id=DEMO_CLIENT_ID, db_path=isolated_db) == []
    with db.connect(isolated_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM interviews WHERE client_id=?", (DEMO_CLIENT_ID,)
            ).fetchone()[0]
            == 0
        )


def _legacy_demo_run(db_path: str, config: dict[str, object]) -> str:
    created = runs.create_run(
        "legacy demo poison",
        "standard",
        "openai",
        config,
        RunCreateOptions(client_id="ordinary-owner", db_path=db_path),
    )
    with db.transaction(db_path) as conn:
        conn.execute(
            "UPDATE runs SET client_id=?, status='completed', completed_at=1 WHERE id=?",
            (DEMO_CLIENT_ID, created.id),
        )
    return created.id


def test_demo_catalog_and_example_copy_reject_forged_noncanonical_source(
    isolated_db: str,
) -> None:
    demos = _seed(isolated_db)
    canonical = demos[0]
    forged_id = _legacy_demo_run(isolated_db, canonical.config)

    client = TestClient(app)
    listed = client.get("/api/runs/demo").json()["runs"]
    assert len(listed) == len(demos)
    assert forged_id not in {run["id"] for run in listed}
    assert client.get(f"/api/runs/{forged_id}").status_code == 404
    client.close()
    with pytest.raises(ValueError, match="example not found"):
        open_example_chat(forged_id, "ordinary-owner")


def test_seed_repairs_tampered_canonical_interview_and_removes_legacy_rows(
    isolated_db: str,
) -> None:
    demos = _seed(isolated_db)
    canonical = demos[0]
    interview_id = str(canonical.config["interview_id"])
    snapshot = seed._read_snapshot()
    expected_interview = next(
        row for row in snapshot["tables"]["interviews"] if row["id"] == interview_id
    )
    expected_turns = [
        row for row in snapshot["tables"]["interview_turns"] if row["interview_id"] == interview_id
    ]
    unknown_interview = interviews.create_interview(
        "ordinary-owner", "legacy demo interview", db_path=isolated_db
    )
    forged_config = {
        **canonical.config,
        "interview_id": unknown_interview["id"],
    }
    legacy_id = _legacy_demo_run(isolated_db, forged_config)
    with db.transaction(isolated_db) as conn:
        conn.execute(
            "UPDATE interviews SET fields_json=? WHERE id=?",
            (json.dumps({"research_challenge": "poison"}), interview_id),
        )
        conn.execute(
            "UPDATE interviews SET client_id=? WHERE id=?",
            (DEMO_CLIENT_ID, unknown_interview["id"]),
        )

    asyncio.run(seed.seed_demo_runs(isolated_db))

    repaired = interviews.get_interview(interview_id, db_path=isolated_db)
    assert repaired is not None
    assert repaired["client_id"] == DEMO_CLIENT_ID
    assert repaired["status"] == expected_interview["status"]
    assert repaired["fields"] == json.loads(expected_interview["fields_json"])
    assert [
        (turn["role"], turn["content"], turn["reasoning"], turn["questions"])
        for turn in repaired["turns"]
    ] == [
        (
            turn["role"],
            turn["content"],
            turn["reasoning"],
            json.loads(turn["questions_json"] or "[]"),
        )
        for turn in expected_turns
    ]
    assert runs.get_run(legacy_id, db_path=isolated_db) is None
    assert interviews.get_interview(unknown_interview["id"], db_path=isolated_db) is None
    assert len(runs.list_runs(client_id=DEMO_CLIENT_ID, db_path=isolated_db)) == len(demos)


def test_public_demo_reads_and_ordinary_example_copies_remain_available(
    isolated_db: str,
) -> None:
    demos = _seed(isolated_db)
    client = TestClient(app)
    public = client.get("/api/runs/demo")
    assert public.status_code == 200
    assert len(public.json()["runs"]) == len(demos)
    client.close()

    copy = open_example_chat(demos[0].id, "ordinary-owner")
    assert copy["client_id"] == "ordinary-owner"
    assert copy["id"] != demos[0].config["interview_id"]
