from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from co_scientist.domains.chat import seed
from co_scientist.domains.chat.repository import examples, interviews
from co_scientist.domains.report import repository as reports
from co_scientist.domains.research_state.repository import hypotheses, records
from co_scientist.platform import db
from co_scientist.platform.db import runs
from co_scientist.platform.db.models import DEMO_CLIENT_ID

from tests._client import make_client


def _example(db_path: str) -> str:
    asyncio.run(seed.seed_demo_runs(db_path))
    return runs.list_runs(client_id=DEMO_CLIENT_ID, db_path=db_path)[0].id


def test_private_copy_preserves_lineage_reviews_evidence_report_and_no_tasks(
    isolated_db: str,
) -> None:
    source_id = _example(isolated_db)
    chat = examples.open_example_chat(source_id, "owner")
    clone_id = interviews.run_id_for_interview(chat["id"], "owner")
    assert clone_id and clone_id != source_id
    source_hyps = hypotheses.list_hypotheses(source_id)
    clone_hyps = hypotheses.list_hypotheses(clone_id)
    assert len(source_hyps) == len(clone_hyps)
    old_ids = {h["id"] for h in source_hyps}
    new_ids = {h["id"] for h in clone_hyps}
    assert not old_ids & new_ids
    assert all(not h["parent_id"] or h["parent_id"] in new_ids for h in clone_hyps)
    assert {h["elo_rating"] for h in source_hyps} == {h["elo_rating"] for h in clone_hyps}
    for reader in (
        records.list_evidence,
        records.list_reviews,
        records.list_matches,
        records.list_claim_evidence,
    ):
        assert len(reader(source_id)) == len(reader(clone_id))
    report = reports.get_latest_report(clone_id)
    assert report and "Curated demonstration only" in report["markdown_text"]
    assert not any(identity in json.dumps(report) for identity in old_ids)
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM scientific_tasks WHERE run_id=?",
                (clone_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM run_credentials WHERE run_id=?",
                (clone_id,),
            ).fetchone()[0]
            == 0
        )


def test_updated_example_opens_a_current_copy_without_overwriting_the_old_chat(
    isolated_db: str,
) -> None:
    source_id = _example(isolated_db)
    old_chat = examples.open_example_chat(source_id, "owner")
    old_run_id = interviews.run_id_for_interview(old_chat["id"], "owner")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE runs SET config_json=json_set(config_json, '$.example_chat_version', 1) "
            "WHERE id=?",
            (old_run_id,),
        )
    current_chat = examples.open_example_chat(source_id, "owner")
    assert current_chat["id"] != old_chat["id"]
    assert interviews.get_interview(old_chat["id"]) is not None
    assert examples.open_example_chat(source_id, "owner")["id"] == current_chat["id"]


def test_all_curated_example_summaries_match_the_generation_contract(isolated_db: str) -> None:
    from co_scientist.platform.llm.structured.validate import validate_json_schema
    from co_scientist.science.schemas.synthesis import KNOWLEDGE_BASE_THEME_SCHEMA

    asyncio.run(seed.seed_demo_runs(isolated_db))
    summary_schema = KNOWLEDGE_BASE_THEME_SCHEMA["schema"]["properties"]["sections"]["items"][
        "properties"
    ]["summary"]
    for source in runs.list_runs(client_id=DEMO_CLIENT_ID):
        chat = examples.open_example_chat(source.id, "owner")
        run_id = interviews.run_id_for_interview(chat["id"], "owner")
        assert run_id
        report = reports.get_latest_report(run_id)
        assert report
        topics = report["payload"]["knowledge_base"]
        assert len(topics) == 6
        overview_topics = report["payload"]["research_overview"]["knowledge_base"]
        assert [(t["title"], t["summary"]) for t in topics] == [
            (t["title"], t["summary"]) for t in overview_topics
        ]
        for topic in topics:
            assert len(topic["summary"].split()) <= 20
            assert ";" not in topic["summary"]
            validate_json_schema(
                {"summary": topic["summary"]},
                {"type": "object", "properties": {"summary": summary_schema}},
            )
            assert topic["summary"] in report["markdown_text"]


def test_saved_example_report_projects_only_unchanged_v15_summaries(isolated_db: str) -> None:
    source_id = "8b8b5183-6667-441c-8991-2eb8d35c7ec4"
    old_summaries = {
        "Dopamine-gated microglial surveillance selects a plastic synapse subset": (
            "Reward-linked dopaminergic input during adolescence may change microglial "
            "surveillance of a subset of frontal synapses, thereby coupling salient experience "
            "to later circuit flexibility rather than globally increasing pruning. The "
            "experiment is designed to distinguish the nominated mediator from correlated "
            "changes in the surrounding model system rather than treating an association as "
            "causal evidence."
        ),
        "Complement tagging encodes synapse history rather than bulk elimination": (
            "Complement-associated tagging may mark synapses with a particular recent activity "
            "history for refinement, making activity-tagged synapse identity a better predictor "
            "of engulfment than total spine density. The experiment is designed to distinguish "
            "the nominated mediator from correlated changes in the surrounding model system "
            "rather than treating an association as causal evidence."
        ),
        "Inhibitory maturation sets the behavioral consequence of pruning": (
            "The effect of adolescent excitatory-synapse refinement on flexibility may depend on "
            "concurrent maturation of local inhibition; altering pruning without measuring "
            "excitation-inhibition balance could conflate two linked developmental processes. "
            "The experiment is designed to distinguish the nominated mediator from correlated "
            "changes in the surrounding model system rather than treating an association as "
            "causal evidence."
        ),
    }
    asyncio.run(seed.seed_demo_runs(isolated_db))
    source_report = reports.get_latest_report(source_id, db_path=isolated_db)
    assert source_report
    current_summaries = {
        item["title"]: item["summary"] for item in source_report["payload"]["knowledge_base"]
    }

    def make_stale_copy(owner: str) -> tuple[str, str]:
        chat = examples.open_example_chat(source_id, owner)
        run_id = interviews.run_id_for_interview(chat["id"], owner)
        assert run_id
        report = reports.get_latest_report(run_id, db_path=isolated_db)
        assert report
        payload = report["payload"]
        for item in payload["knowledge_base"]:
            if item["title"] in old_summaries:
                item["summary"] = old_summaries[item["title"]]
        for item in payload["research_overview"]["knowledge_base"]:
            if item["title"] in old_summaries:
                item["summary"] = old_summaries[item["title"]]
        markdown = report["markdown_text"]
        for title, summary in old_summaries.items():
            markdown = markdown.replace(current_summaries[title], summary)
        with db.transaction(isolated_db) as conn:
            config = json.loads(
                conn.execute("SELECT config_json FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            )
            config["example_chat_version"] = 15
            config["demo_seed_version"] = 15
            conn.execute("UPDATE runs SET config_json=? WHERE id=?", (json.dumps(config), run_id))
            payload["demo_seed_version"] = 15
            conn.execute(
                "UPDATE reports SET payload_json=?, markdown_text=? WHERE id=?",
                (json.dumps(payload), markdown, report["id"]),
            )
        return run_id, report["id"]

    run_id, report_id = make_stale_copy("owner")
    edited_run_id, _ = make_stale_copy("edited-owner")
    with db.connect(isolated_db) as conn:
        edited_row = conn.execute(
            "SELECT id, payload_json FROM reports WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (edited_run_id,),
        ).fetchone()
    assert edited_row
    edited_payload = json.loads(edited_row["payload_json"])
    edited_title = next(iter(old_summaries))
    next(item for item in edited_payload["knowledge_base"] if item["title"] == edited_title)[
        "summary"
    ] = "Owner's edited summary."
    next(
        item
        for item in edited_payload["research_overview"]["knowledge_base"]
        if item["title"] == edited_title
    )["summary"] = "Owner's edited summary."
    detail_title, reference_title = list(old_summaries)[1:]
    for collection in (
        edited_payload["knowledge_base"],
        edited_payload["research_overview"]["knowledge_base"],
    ):
        by_title = {item["title"]: item for item in collection}
        by_title[detail_title]["detail"] += " Owner's additional detail."
    by_title = {item["title"]: item for item in edited_payload["knowledge_base"]}
    by_title[reference_title]["reference_ids"] = ["owner-edited-reference"]
    by_title = {
        item["title"]: item for item in edited_payload["research_overview"]["knowledge_base"]
    }
    by_title[reference_title]["references"][0]["title"] = "Owner-edited reference title."
    with db.transaction(isolated_db) as conn:
        conn.execute(
            "UPDATE reports SET payload_json=? WHERE id=?",
            (json.dumps(edited_payload), edited_row["id"]),
        )

    with db.connect(isolated_db) as conn:
        stored_before = tuple(
            conn.execute(
                "SELECT payload_json, markdown_text FROM reports WHERE id=?", (report_id,)
            ).fetchone()
        )

    client = make_client()
    headers = {"X-Client-ID": "owner"}
    response = client.get(f"/api/runs/{run_id}/report", headers=headers)
    markdown_response = client.get(f"/api/runs/{run_id}/report.md", headers=headers)
    assert response.status_code == markdown_response.status_code == 200
    projected = response.json()
    assert projected["id"] == report_id
    assert {
        item["title"]: item["summary"] for item in projected["payload"]["knowledge_base"]
    } == current_summaries
    overview = projected["payload"]["research_overview"]["knowledge_base"]
    assert {item["title"]: item["summary"] for item in overview} == current_summaries
    projected_without_summary_changes = json.loads(json.dumps(projected["payload"]))
    for collection in (
        projected_without_summary_changes["knowledge_base"],
        projected_without_summary_changes["research_overview"]["knowledge_base"],
    ):
        for item in collection:
            if item["title"] in old_summaries:
                item["summary"] = old_summaries[item["title"]]
    assert projected_without_summary_changes == json.loads(stored_before[0])
    markdown = markdown_response.text
    knowledge_base = markdown.split("## Knowledge Base", 1)[1]
    for title, old_summary in old_summaries.items():
        assert current_summaries[title] in knowledge_base
        assert old_summary not in knowledge_base

    edited_response = client.get(
        f"/api/runs/{edited_run_id}/report", headers={"X-Client-ID": "edited-owner"}
    )
    assert edited_response.status_code == 200
    edited_topic = next(
        item
        for item in edited_response.json()["payload"]["knowledge_base"]
        if item["title"] == edited_title
    )
    assert edited_topic["summary"] == "Owner's edited summary."
    edited_topics = {
        item["title"]: item for item in edited_response.json()["payload"]["knowledge_base"]
    }
    assert edited_topics[detail_title]["summary"] == old_summaries[detail_title]
    assert edited_topics[detail_title]["detail"].endswith("Owner's additional detail.")
    assert edited_topics[reference_title]["summary"] == old_summaries[reference_title]
    assert edited_topics[reference_title]["reference_ids"] == ["owner-edited-reference"]
    edited_overview = {
        item["title"]: item
        for item in edited_response.json()["payload"]["research_overview"]["knowledge_base"]
    }
    assert edited_overview[reference_title]["references"][0]["title"] == (
        "Owner-edited reference title."
    )

    with db.connect(isolated_db) as conn:
        stored_after = tuple(
            conn.execute(
                "SELECT payload_json, markdown_text FROM reports WHERE id=?", (report_id,)
            ).fetchone()
        )
    assert stored_after == stored_before


@pytest.mark.parametrize("version", [15, 16])
def test_every_saved_example_learning_follows_current_summaries(
    isolated_db: str, version: int
) -> None:
    legacy = json.loads(
        (Path(__file__).parent / f"fixtures/example_v{version}_summaries.json").read_text()
    )
    asyncio.run(seed.seed_demo_runs(isolated_db))
    client = make_client()
    for source_id, summaries in legacy.items():
        chat = examples.open_example_chat(source_id, "owner")
        run_id = interviews.run_id_for_interview(chat["id"], "owner")
        assert run_id
        report = reports.get_latest_report(run_id)
        assert report
        expected = {
            topic["title"]: topic["summary"] for topic in report["payload"]["knowledge_base"]
        }
        payload = report["payload"]
        markdown = report["markdown_text"]
        for collection in (
            payload["knowledge_base"],
            payload["research_overview"]["knowledge_base"],
        ):
            for topic in collection:
                topic["summary"] = summaries[topic["title"]]
        for title, summary in summaries.items():
            markdown = markdown.replace(expected[title], summary)
        payload["demo_seed_version"] = version
        with db.transaction() as conn:
            conn.execute(
                "UPDATE runs SET config_json=json_set(config_json, '$.example_chat_version', ?, "
                "'$.demo_seed_version', ?) WHERE id=?",
                (version, version, run_id),
            )
            conn.execute(
                "UPDATE reports SET payload_json=?, markdown_text=? WHERE id=?",
                (json.dumps(payload), markdown, report["id"]),
            )
        response = client.get(f"/api/runs/{run_id}/report", headers={"X-Client-ID": "owner"})
        assert response.status_code == 200
        projected = response.json()
        for collection in (
            projected["payload"]["knowledge_base"],
            projected["payload"]["research_overview"]["knowledge_base"],
        ):
            assert {topic["title"]: topic["summary"] for topic in collection} == expected
        for title, summary in expected.items():
            assert summary in projected["markdown_text"]
            assert (
                summaries[title]
                not in projected["markdown_text"].split("## Knowledge Base\n", 1)[1]
            )
        stored = reports.get_latest_report(run_id)
        assert stored and stored["payload"] == payload
