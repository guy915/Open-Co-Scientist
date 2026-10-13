from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from evaluations import paired_artifacts, paired_quality
from evaluations._identity import identity_digest
from evaluations._paired_db import read_snapshot
from evaluations.quality_goals import GOAL_VERSION, GOALS
from evaluations.tests.test_paired_quality import fake_database


def archive(
    path: Path, goal_id: str, *, branch: bool = False, change: str = "", provider: str = "free"
) -> None:
    db = path.with_suffix(".db")
    fake_database(db, goal_id, branch=branch)
    source, count = ("b" if branch else "a") * 40, 20 if branch else 100
    with sqlite3.connect(db) as conn:
        if change == "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE evaluation_runs "
            "(run_id, source_commit, physical_requests, request_ceiling, "
            "counter_kind, request_counter_sha256)"
        )
        conn.execute(
            "INSERT INTO evaluation_runs VALUES ('r',?,?,200,'http_transport_attempts',?)",
            (source, count, "f" * 64),
        )
        if provider != "free":
            config = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
            identity = config["evaluation_identity"]
            identity["configured_models"] = dict.fromkeys(
                ("worker", "supervisor", "chat", "safety", "claim_verifier"),
                provider + "/fixture-model",
            )
            identity["execution_environment"].update(
                COSCIENTIST_REQUIRE_FREE_MODELS="0",
                COSCIENTIST_BENCHMARK_PROVIDER=provider,
                COSCIENTIST_BENCHMARK_POLICY_SHA256="c" * 64,
                COSCIENTIST_BENCHMARK_ENDPOINT_SHA256="d" * 64,
            )
            identity.pop("digest")
            identity["digest"] = identity_digest(identity)
            conn.execute("UPDATE runs SET config_json=?", (json.dumps(config),))
        if change == "control":
            config = json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0])
            identity = config["evaluation_identity"]
            identity["configured_models"]["worker"] = "openrouter/other-free-model"
            identity.pop("digest")
            identity["digest"] = identity_digest(identity)
            conn.execute("UPDATE runs SET config_json=?", (json.dumps(config),))
        if change == "legacy":
            conn.execute("ALTER TABLE evaluation_runs DROP COLUMN counter_kind")
            conn.execute("ALTER TABLE evaluation_runs DROP COLUMN request_counter_sha256")
        if change == "counter":
            conn.execute("UPDATE evaluation_runs SET request_counter_sha256=?", ("e" * 64,))
    snapshot = read_snapshot(db, "r", goal_id, source)
    receipt: dict[str, Any] = {
        "goal_version": GOAL_VERSION,
        "goal_id": goal_id,
        "goal": GOALS[goal_id],
        "source_commit": source,
        "physical_requests_attempted": count,
        "physical_request_ceiling": 200,
        "offline_disclaimer": None,
        "evaluation_identity": snapshot.identity,
        "metrics": snapshot.metrics,
        "provenance": {"source": {"git_commit": source, "git_dirty": False}},
    }
    if change == "count":
        receipt["physical_requests_attempted"] = count + 1
    elif change == "ceiling":
        receipt["physical_request_ceiling"] = 201
    elif change == "offline":
        receipt["offline_disclaimer"] = "offline"
    elif change == "source":
        receipt["source_commit"] = "c" * 40
    elif change == "metric":
        receipt["metrics"]["supported_claims"] = 999
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.write(db, "benchmark-artifacts/snapshot.db")
        wal = db.with_name(db.name + "-wal")
        if wal.exists():
            bundle.write(wal, "benchmark-artifacts/snapshot.db-wal")
        if change != "missing":
            bundle.writestr("benchmark-artifacts/receipt.json", json.dumps(receipt))
        if change == "duplicate":
            bundle.writestr("receipt.json", json.dumps(receipt))
        bundle.writestr("../outside.txt", "never extract")


def cohort(tmp_path: Path, *, change: str = "", provider: str = "free") -> dict[str, list[Path]]:
    arms: dict[str, list[Path]] = {"main": [], "branch": []}
    for arm in arms:
        for index, goal in enumerate(GOALS):
            path = tmp_path / f"{arm}-{index}.zip"
            archive(
                path,
                goal,
                branch=arm == "branch",
                change=change if index == 0 else "",
                provider=provider,
            )
            arms[arm].append(path)
    return arms


@pytest.mark.parametrize("provider", ["anthropic", "azure"])
def test_paid_cohort_is_explicit_and_replays_both_judge_orders_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    def deny(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access in paid artifact replay")

    monkeypatch.setattr(socket.socket, "connect", deny)
    arms = cohort(tmp_path, provider=provider)
    hashes = {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for paths in arms.values() for p in paths
    }
    with pytest.raises(ValueError, match="require free models"):
        paired_artifacts.prepare(arms, tmp_path / "free")
    manifest = paired_artifacts.prepare(arms, tmp_path / "paid", provider=provider)
    pairs = paired_quality.load_pairs(manifest)
    orders = 0

    def judge(packet: dict[str, str]) -> dict[str, str]:
        nonlocal orders
        orders += 1
        return {"winner": "tie", "rationale": "Recorded paid-provider wiring fixture."}

    rows = [paired_quality.compare(main, branch, judge) for main, branch in pairs]
    summary = paired_quality.summarize(rows)
    report = {
        "pairs": rows,
        "summary": summary,
        "judge_mode": "recorded",
        "judge_physical_requests": 0,
    }
    (tmp_path / "paired.json").write_text(json.dumps(report))
    (tmp_path / "paired.md").write_text(paired_quality.markdown(report))
    assert orders == 6 and summary["goal_pairs"] == 3
    assert "inside the noise" in (tmp_path / "paired.md").read_text()
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in hashes.items())


def test_free_cohort_cannot_be_relabeled_as_paid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="paid provider controls"):
        paired_artifacts.prepare(cohort(tmp_path), tmp_path / "paid", provider="anthropic")


def test_paid_cohort_rejects_incompatible_provider_before_judging(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="paid provider controls"):
        paired_artifacts.prepare(
            cohort(tmp_path, provider="azure"), tmp_path / "paid", provider="anthropic"
        )


@pytest.mark.parametrize("change", ["", "wal"])
def test_recorded_archives_replay_full_pipeline_without_network_or_input_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    def deny_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network call in recorded artifact pipeline")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    arms = cohort(tmp_path, change=change)
    hashes = {
        p: hashlib.sha256(p.read_bytes()).hexdigest() for paths in arms.values() for p in paths
    }
    inputs = tmp_path / "archives.json"
    inputs.write_text(json.dumps({a: [p.name for p in paths] for a, paths in arms.items()}))
    output = tmp_path / "replay"
    monkeypatch.setattr(
        sys, "argv", ["paired_artifacts", "--archives", str(inputs), "--output", str(output)]
    )
    assert paired_artifacts.main() == 0
    manifest = output / "prepared/pairs.json"
    exported = output / "packets.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["paired_quality", str(manifest), "--export-packets", "--output", str(exported)],
    )
    assert paired_quality.main() == 0
    judgments = output / "judgments.json"
    judgments.write_text(
        json.dumps(
            {
                key: {"winner": "tie", "rationale": "recorded fake"}
                for key in json.loads(exported.read_text())
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "paired_quality",
            str(manifest),
            "--judgments",
            str(judgments),
            "--output",
            str(output / "paired"),
        ],
    )
    assert paired_quality.main() == 0
    result = json.loads((output / "paired.json").read_text())
    assert result["summary"]["goal_pairs"] == 3
    assert result["summary"]["judge_orders"] == 6
    assert result["judge_physical_requests"] == 0
    assert all(row["main"]["physical_calls"] == 100 for row in result["pairs"])
    assert "inside the noise" in (output / "paired.md").read_text()
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in hashes.items())
    assert not (output / "prepared/outside.txt").exists()


@pytest.mark.parametrize(
    "change", ["count", "ceiling", "offline", "source", "metric", "missing", "duplicate", "legacy"]
)
def test_inconsistent_or_missing_receipt_fails_before_judging(tmp_path: Path, change: str) -> None:
    with pytest.raises(ValueError):
        paired_artifacts.prepare(cohort(tmp_path, change=change), tmp_path / "prepared")


def test_duplicate_goals_are_rejected(tmp_path: Path) -> None:
    arms = cohort(tmp_path)
    other = tmp_path / "duplicate-goal.zip"
    archive(other, "cell-biology")
    arms["main"][1] = other
    with pytest.raises(ValueError, match="duplicate goal"):
        paired_artifacts.prepare(arms, tmp_path / "prepared")


def test_mismatched_controls_are_rejected_before_judging(tmp_path: Path) -> None:
    arms = cohort(tmp_path)
    changed = tmp_path / "different-controls.zip"
    archive(changed, "cell-biology", branch=True, change="control")
    arms["branch"][0] = changed
    with pytest.raises(ValueError, match="configured_models"):
        paired_artifacts.prepare(arms, tmp_path / "prepared")


def test_changed_http_instrumentation_is_rejected_before_judging(tmp_path: Path) -> None:
    arms = cohort(tmp_path)
    changed = tmp_path / "different-counter.zip"
    archive(changed, "cell-biology", branch=True, change="counter")
    arms["branch"][0] = changed
    with pytest.raises(ValueError, match="instrumentation differs"):
        paired_artifacts.prepare(arms, tmp_path / "prepared")


def test_malformed_download_ids_and_duplicate_runs_are_rejected_before_network(
    tmp_path: Path,
) -> None:
    for value in ("1,2", "1,2,3,4", "1,2,--help", "1,2,0", "1,2,3/zip"):
        with pytest.raises(ValueError):
            paired_artifacts.parse_run_ids(value)
    with pytest.raises(ValueError, match="distinct"):
        paired_artifacts.download(
            "fixture-owner/research",
            {"main": ["1", "2", "3"], "branch": ["1", "2", "3"]},
            tmp_path / "download",
        )


@pytest.mark.parametrize("explicit", [False, True])
def test_download_cli_uses_current_repository_for_the_whole_recorded_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    arms = cohort(tmp_path)
    archives = dict(zip(range(1, 7), arms["main"] + arms["branch"], strict=True))
    repository = "launch-owner/Research-Workbench"
    monkeypatch.setenv("GITHUB_REPOSITORY", "wrong/environment" if explicit else repository)
    routes: list[str] = []

    def check_output(command: list[str], *, timeout: int) -> bytes:
        assert command[:2] == ["gh", "api"]
        route = command[2]
        assert route.startswith(f"repos/{repository}/actions/runs/")
        routes.append(route)
        run_id = int(route.split("/")[5])
        if route.endswith("/artifacts"):
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": run_id,
                            "name": "benchmark-express-fixture",
                            "expired": False,
                            "size_in_bytes": archives[run_id].stat().st_size,
                        }
                    ]
                }
            ).encode()
        return json.dumps(
            {
                "path": ".github/workflows/benchmark.yml",
                "event": "workflow_dispatch",
                "status": "completed",
            }
        ).encode()

    def run(
        command: list[str], *, stdout: BinaryIO, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert command[:2] == ["gh", "api"]
        route = command[2]
        assert route.startswith(f"repos/{repository}/actions/artifacts/")
        assert route.endswith("/zip")
        routes.append(route)
        stdout.write(archives[int(route.split("/")[5])].read_bytes())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "run", run)
    output = tmp_path / "outputs"
    argv = [
        "paired-artifacts",
        "--download",
        "--main-runs",
        "1,2,3",
        "--branch-runs",
        "4,5,6",
        "--output",
        str(output),
    ]
    if explicit:
        argv.extend(["--repository", repository])
    monkeypatch.setattr(sys, "argv", argv)
    assert paired_artifacts.main() == 0
    pairs = paired_quality.load_pairs(output / "prepared" / "pairs.json")
    assert len(pairs) == 3
    assert len(routes) == 18
    assert all(route.startswith(f"repos/{repository}/") for route in routes)


@pytest.mark.parametrize(
    "repository", [None, "", "--help", "owner/repo/extra", "https://github.com"]
)
def test_download_cli_rejects_missing_or_invalid_repository_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: str | None
) -> None:
    if repository is None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", repository)

    def deny_request(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("request before repository validation")

    monkeypatch.setattr(subprocess, "check_output", deny_request)
    output = tmp_path / "outputs"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "paired-artifacts",
            "--download",
            "--main-runs",
            "1,2,3",
            "--branch-runs",
            "4,5,6",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(ValueError, match="invalid repository"):
        paired_artifacts.main()
    assert not output.exists()
