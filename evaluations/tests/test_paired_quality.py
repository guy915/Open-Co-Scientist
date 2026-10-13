from __future__ import annotations

import dataclasses
import hashlib
import json
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from evaluations import paired_quality
from evaluations._identity import identity_digest
from evaluations._paired_db import Snapshot, identity_digest_goal, read_snapshot
from evaluations._paired_judge import judge_pair, packets, recorded_judge
from evaluations.quality_goals import GOALS


@pytest.mark.parametrize(
    ("flag", "value"),
    [("COSCIENTIST_FORCE_OFFLINE", "1"), ("COSCIENTIST_TEST_DOUBLE", "deterministic")],
)
def test_live_judge_rejects_current_and_legacy_test_selection_before_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str, value: str
) -> None:
    monkeypatch.setenv(flag, value)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "paired_quality",
            str(tmp_path / "absent.json"),
            "--live-judge",
            "--output",
            str(tmp_path / "output.json"),
        ],
    )
    with pytest.raises(ValueError, match="live judge cannot use"):
        paired_quality.main()
    assert not (tmp_path / "output.json").exists()


def fake_database(path: Path, goal_id: str, *, branch: bool = False) -> None:
    identity = {
        "version": 1,
        "backend": "real",
        "resolved_config": {},
        "goal_sha256": identity_digest_goal(GOALS[goal_id]),
        "configured_models": {"worker": "openrouter/free-model"},
        "tools": {"mcp_endpoint_sha256": "same"},
        "execution_environment": {"COSCIENTIST_REQUIRE_FREE_MODELS": "1"},
    }
    identity["digest"] = identity_digest(identity)
    usage = {
        "model": {
            "calls": 20 if branch else 100,
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "reported_usage_calls": 20 if branch else 100,
            "observed_model_calls": 20 if branch else 100,
        }
    }
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE runs (id, research_goal, config_json, profile, status, llm_backend);
            CREATE TABLE reports (id, run_id, markdown_text, created_at);
            CREATE TABLE hypotheses (id, run_id, title);
            CREATE TABLE hypothesis_state (hypothesis_id, verification_verdict);
            CREATE TABLE claim_evidence (run_id, hypothesis_id, claim, label);
            CREATE TABLE run_metrics (run_id, metrics_json);
            CREATE TABLE scientific_tasks (run_id, started_at, completed_at);
        """)
        conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "r",
                GOALS[goal_id],
                json.dumps({"evaluation_identity": identity}),
                "express",
                "completed",
                "real",
            ),
        )
        markdown = (
            "_Provider: **engine**_\n_Prepared by Co-Scientist on 2026-10-08._\n"
            "## Top hypotheses\n### 1. **Co-Scientist - Idea one** _Elo: 1200_\n"
            "Testable mechanism.\n### 2. **Co-Scientist - Idea two** _Elo: 1100_\n"
            + ("screened, not deep-verified\n" if branch else "Testable mechanism two.\n")
            + "## Main research directions\nIdea hidden is a direction example, not an entry.\n"
        )
        conn.execute("INSERT INTO reports VALUES ('latest','r',?,2)", (markdown,))
        conn.execute("INSERT INTO reports VALUES ('old','r','old report',1)")
        conn.executemany(
            "INSERT INTO hypotheses VALUES (?, 'r', ?)",
            [
                ("h1", "Idea one"),
                ("h2", "Idea two"),
                ("hidden", "Idea hidden"),
            ],
        )
        conn.executemany(
            "INSERT INTO hypothesis_state VALUES (?,?)",
            [
                ("h1", "supported"),
                ("h2", None if branch else "unverified"),
                ("hidden", "undermined"),
            ],
        )
        conn.executemany(
            "INSERT INTO claim_evidence VALUES ('r',?,?,?)",
            [
                ("h1", "claim one", "supports"),
                ("h1", "claim two", "partial"),
                ("h1", "claim one", "supports"),
                ("hidden", "claim three", "supports"),
                ("h2", "claim four", "insufficient"),
            ],
        )
        conn.execute(
            "INSERT INTO run_metrics VALUES ('r',?)", (json.dumps({"model_usage": usage}),)
        )
        conn.executemany(
            "INSERT INTO scientific_tasks VALUES (?, ?, ?)",
            [
                ("r", 0, 15 if branch else 50),
                ("r", 5, 10),
                ("other", 0, 9999),
            ],
        )


def snapshot(tmp_path: Path, *, branch: bool = False) -> Snapshot:
    db = tmp_path / ("branch.db" if branch else "main.db")
    fake_database(db, "cell-biology", branch=branch)
    return read_snapshot(db, "r", "cell-biology", "a" * 40)


@pytest.mark.parametrize("branch", [False, True])
def test_duplicate_delivered_titles_refuse_to_credit_multiple_hypotheses(
    tmp_path: Path, branch: bool
) -> None:
    db = tmp_path / "duplicate.db"
    fake_database(db, "cell-biology", branch=branch)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE hypotheses SET title='Idea two' WHERE id='hidden'")
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="ambiguous delivered hypothesis identity"):
        read_snapshot(db, "r", "cell-biology", "a" * 40)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_duplicate_undelivered_titles_do_not_inflate_report_counts(tmp_path: Path) -> None:
    db = tmp_path / "undelivered.db"
    fake_database(db, "cell-biology")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO hypotheses VALUES ('other', 'r', 'Idea hidden')")
    facts = read_snapshot(db, "r", "cell-biology", "a" * 40).metrics
    assert (facts["ideas_generated"], facts["ideas_delivered"]) == (4, 2)
    assert facts["delivered_ids"] == ["h1", "h2"]
    assert facts["delivered_supported_claims"] == 2


def test_counts_report_entries_not_pool_mentions_and_deduplicates_claims(tmp_path: Path) -> None:
    m = snapshot(tmp_path, branch=True).metrics
    assert (m["ideas_generated"], m["ideas_delivered"]) == (3, 2)
    assert (m["ideas_featured"], m["ideas_screened"]) == (1, 1)
    assert (m["supported_claims"], m["delivered_supported_claims"], m["claims_assessed"]) == (
        3,
        2,
        4,
    )
    assert m["verification_verdict_mix"] == {"supported": 1, "not_assessed": 1, "undermined": 1}
    assert (m["wall_seconds"], m["physical_calls"], m["total_tokens"]) == (15, 20, 1200)


@pytest.mark.parametrize(
    "answers,winner,disagreement",
    [
        (("A", "B"), "main", False),
        (("B", "A"), "branch", False),
        (("A", "A"), "tie", True),
        (("B", "B"), "tie", True),
        (("tie", "tie"), "tie", False),
        (("tie", "A"), "tie", True),
    ],
)
def test_swapped_orders_resolve_position_bias(
    answers: tuple[str, str],
    winner: str,
    disagreement: bool,
) -> None:
    responses = iter(answers)
    seen = []

    def judge(packet: dict[str, str]) -> dict[str, Any]:
        seen.append(packet)
        return {"winner": next(responses), "rationale": "synthetic recorded judgment"}

    result = judge_pair("goal", "_Provider: secret\nreport one", "report two", judge)
    assert result["winner"] == winner
    assert result["order_disagreement"] is disagreement
    assert seen[0]["A"] == seen[1]["B"] == "report one"
    assert seen[0]["B"] == seen[1]["A"] == "report two"
    assert set(seen[0]) == {"rubric", "goal", "A", "B"}


def test_missing_or_invalid_judgment_never_becomes_a_tie() -> None:
    with pytest.raises(ValueError, match="missing judgment"):
        judge_pair("goal", "one", "two", recorded_judge({}))
    with pytest.raises(ValueError, match="judge must"):
        judge_pair("goal", "one", "two", lambda _: {"winner": "invalid"})
    assert judge_pair("goal", "", "two", recorded_judge({}))["winner"] is None


def test_three_wins_are_inside_noise_and_do_not_establish_equivalence(tmp_path: Path) -> None:
    a, b = snapshot(tmp_path), snapshot(tmp_path, branch=True)
    responses = iter(["B", "A"] * 3)
    rows = [
        paired_quality.compare(
            a,
            b,
            lambda _: {
                "winner": next(responses),
                "rationale": "synthetic",
            },
        )
        for _ in GOALS
    ]
    summary = paired_quality.summarize(rows)
    assert (summary["goal_pairs"], summary["judge_orders"]) == (3, 6)
    assert summary["two_sided_sign_test_p"] == 0.25
    assert summary["decision"] == "inconclusive"
    assert "inside the noise" in summary["noise"]


def test_missing_usage_is_unknown_and_flags_review(tmp_path: Path) -> None:
    a, b = snapshot(tmp_path), snapshot(tmp_path, branch=True)
    with sqlite3.connect(tmp_path / "branch.db") as conn:
        conn.execute("UPDATE run_metrics SET metrics_json='{}'")
    b = read_snapshot(tmp_path / "branch.db", "r", "cell-biology", "b" * 40)
    row = paired_quality.compare(a, b, lambda _: {"winner": "tie", "rationale": "synthetic"})
    assert b.metrics["total_tokens"] is None
    assert b.metrics["physical_calls"] is None
    assert "efficiency telemetry incomplete" in row["concerns"]


def test_control_mismatch_and_reduced_delivery_require_attention(tmp_path: Path) -> None:
    a, b = snapshot(tmp_path), snapshot(tmp_path, branch=True)
    bad = dataclasses.replace(b, identity={**b.identity, "configured_models": {"worker": "other"}})
    with pytest.raises(ValueError, match="configured_models"):
        paired_quality.compare(a, bad, recorded_judge({}))
    reduced = dataclasses.replace(
        b, metrics={**b.metrics, "ideas_delivered": 1, "completed": False}
    )
    row = paired_quality.compare(a, reduced, lambda _: {"winner": "tie", "rationale": "synthetic"})
    assert "branch incomplete" in row["concerns"]
    assert "fewer report ideas delivered" in row["concerns"]


def test_changed_stored_config_and_wrong_goal_fail_before_judging(tmp_path: Path) -> None:
    snapshot(tmp_path)
    db = tmp_path / "main.db"
    with pytest.raises(ValueError, match="fixed goal"):
        read_snapshot(db, "r", "urban-hydrology", "a" * 40)
    with sqlite3.connect(db) as conn:
        config = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
        config["max_llm_calls"] = 99
        conn.execute("UPDATE runs SET config_json=?", (json.dumps(config),))
    with pytest.raises(ValueError, match="run config changed"):
        read_snapshot(db, "r", "cell-biology", "a" * 40)


def test_dispatch_receipt_counts_calls_lost_from_failed_task_telemetry(tmp_path: Path) -> None:
    snapshot(tmp_path)
    db = tmp_path / "main.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE evaluation_runs (run_id, source_commit, physical_requests)")
        conn.execute("INSERT INTO evaluation_runs VALUES ('r',?,150)", ("a" * 40,))
    m = read_snapshot(db, "r", "cell-biology", "a" * 40).metrics
    assert m["physical_calls"] is None
    assert m["recorded_backend_invocations"] == 150
    assert m["call_count_basis"] == "backend_invocations"
    assert m["total_tokens"] is None
    assert m["reported_token_subtotal"] == 1200
    with pytest.raises(ValueError, match="source commit differs"):
        read_snapshot(db, "r", "cell-biology", "b" * 40)


def test_entire_pipeline_is_offline_and_does_not_write_run_databases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network call in recorded pipeline")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    entries = []
    judgments = {}
    hashes = {}
    for goal_id, goal in GOALS.items():
        entry: dict[str, Any] = {"goal_id": goal_id}
        reports = []
        for arm in ("main", "branch"):
            db = tmp_path / f"{goal_id}-{arm}.db"
            fake_database(db, goal_id, branch=arm == "branch")
            hashes[db] = hashlib.sha256(db.read_bytes()).hexdigest()
            entry[arm] = {"db": db.name, "run_id": "r", "source_commit": "a" * 40}
            reports.append(read_snapshot(db, "r", goal_id, "a" * 40).report)
        for p in packets(goal, *reports):
            judgments[identity_digest(p)] = {"winner": "tie", "rationale": "recorded fake"}
        entries.append(entry)
    manifest = tmp_path / "pairs.json"
    manifest.write_text(json.dumps({"pairs": entries}))
    recorded = tmp_path / "judgments.json"
    recorded.write_text(json.dumps(judgments))
    output = tmp_path / "result"
    monkeypatch.setattr(
        sys,
        "argv",
        ["paired_quality", str(manifest), "--judgments", str(recorded), "--output", str(output)],
    )
    assert paired_quality.main() == 0
    result = json.loads(output.with_suffix(".json").read_text())
    table = output.with_suffix(".md").read_text()
    assert result["summary"]["research_runs"] == 6
    assert result["summary"]["judge_orders"] == 6
    assert table.count("| main |") == table.count("| branch |") == 3
    assert "inside the noise" in table
    assert all(
        hashlib.sha256(db.read_bytes()).hexdigest() == digest for db, digest in hashes.items()
    )
    manifest.write_text(json.dumps({"pairs": entries[:2]}))
    with pytest.raises(ValueError, match="exactly one pair"):
        paired_quality.load_pairs(manifest)
