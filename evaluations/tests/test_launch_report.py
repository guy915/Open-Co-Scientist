from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from evaluations import launch_report
from evaluations._identity import identity_digest
from evaluations._paired_judge import packets
from evaluations.quality_goals import GOALS
from evaluations.tests.test_paired_artifacts import archive


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access in launch report tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


def manifest(tmp_path: Path, *, change: str = "") -> Path:
    data: dict[str, dict[str, str | None]] = {}
    for goal in launch_report.GOALS:
        data[goal] = {}
        for arm in ("main", "branch"):
            path = tmp_path / f"{goal}-{arm}.zip"
            archive(path, goal, branch=arm == "branch", change=change if arm == "branch" else "")
            data[goal][arm] = path.name
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(data))
    return path


def test_two_goal_archive_pipeline_preserves_bytes_and_swaps_both_orders(tmp_path: Path) -> None:
    source = manifest(tmp_path)
    hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.glob("*.zip")
    }
    snapshots = launch_report.prepare(source, tmp_path / "prepared")
    seen: list[dict[str, str]] = []

    def judge(packet: dict[str, str]) -> dict[str, Any]:
        seen.append(packet)
        return {"winner": "A", "rationale": "Recorded fixture position preference."}

    result = launch_report.report(snapshots, judge)
    assert len(seen) == 4
    assert all(seen[i]["A"] == seen[i + 1]["B"] for i in (0, 2))
    assert result["summary"]["goal_pairs"] == 2
    assert result["summary"]["research_runs"] == 4
    assert result["summary"]["position_disagreements"] == 2
    assert all(row["judgment"]["winner"] == "tie" for row in result["pairs"])
    assert "three-goal sample" not in result["summary"]["noise"]
    table = launch_report.markdown(result)
    assert "urban-hydrology | main | not run" in table
    assert "n=2 goal pairs" in table
    assert hashes == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in hashes}


def test_no_archives_reports_zero_sample_and_never_judges(tmp_path: Path) -> None:
    source = tmp_path / "runs.json"
    source.write_text(
        json.dumps({goal: {"main": None, "branch": None} for goal in launch_report.GOALS})
    )
    snapshots = launch_report.prepare(source, tmp_path / "prepared")

    def deny(packet: dict[str, str]) -> dict[str, Any]:
        raise AssertionError("missing report must not be judged")

    result = launch_report.report(snapshots, deny)
    assert result["summary"]["goal_pairs"] == 0
    assert result["summary"]["judge_orders"] == 0
    assert "No paired quality or efficiency difference was measured" in result["summary"]["noise"]
    assert launch_report.markdown(result).count("| not run | unknown") == 6


@pytest.mark.parametrize("change", ["counter", "metric", "offline"])
def test_bad_receipts_refuse_before_any_judgment(tmp_path: Path, change: str) -> None:
    source = manifest(tmp_path, change=change)
    with pytest.raises(ValueError):
        launch_report.prepare(source, tmp_path / "prepared")


def test_only_completed_main_keeps_observations_without_inventing_a_pair(tmp_path: Path) -> None:
    source = manifest(tmp_path)
    data = json.loads(source.read_text())
    for goal in data:
        data[goal]["branch"] = None
    source.write_text(json.dumps(data))
    snapshots = launch_report.prepare(source, tmp_path / "prepared")
    result = launch_report.report(snapshots, lambda packet: {})
    assert result["summary"]["goal_pairs"] == 0
    table = launch_report.markdown(result)
    assert "cell-biology | main | True" in table
    assert "cell-biology | branch | not run" in table


def test_credential_in_judgment_withholds_final_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = manifest(tmp_path)
    snapshots = launch_report.prepare(source, tmp_path / "packet-inputs")
    secret = "private-result-marker"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    answers = {}
    for goal, arms in snapshots.items():
        for packet in packets(GOALS[goal], arms["main"].report, arms["branch"].report):
            answers[identity_digest(packet)] = {"winner": "tie", "rationale": secret}
    judgments = tmp_path / "judgments.json"
    judgments.write_text(json.dumps(answers))
    output = tmp_path / "result"
    monkeypatch.setattr(
        sys,
        "argv",
        ["launch-report", str(source), "--judgments", str(judgments), "--output", str(output)],
    )
    with pytest.raises(ValueError, match="configured credential"):
        launch_report.main()
    assert not output.with_suffix(".json").exists()
    assert not output.with_suffix(".md").exists()


def test_explicit_live_path_counts_four_fake_http_attempts(tmp_path: Path) -> None:
    source = manifest(tmp_path)
    script = """
import asyncio,json,socket,sys
from evaluations import launch_report,benchmark_transport,_live_config
import httpx
def deny(*args,**kwargs):raise AssertionError('network access')
socket.socket.connect=deny
class Fake:
 def supports_json_schema(self,model):return False
 async def complete(self,**kwargs):
  async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200))) as c:
   return await c.post('https://openrouter.ai/api/v1/chat/completions')
configure=_live_config.configure_live_environment
def configured():
 result=configure()
 from co_scientist.platform.llm.request.backend import install_backend
 install_backend(Fake())
 return result
_live_config.configure_live_environment=configured
benchmark_transport.COUNTER_SHA256='f'*64
def judge(model):
 def call(packet):
  from co_scientist.platform.llm.request.backend import active_backend
  asyncio.run(active_backend().complete(model=model))
  return {'winner':'tie','rationale':'Fake HTTP wiring proof.'}
 return call
launch_report.live_judge=judge
assert launch_report.main()==0
"""
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "live-wiring"
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), "--live-judge", "--output", str(output)],
        cwd=root,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(root),
            "OPENROUTER_API_KEY": "synthetic-router-key",
            "MODEL_NAME": "openrouter/fixture",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.with_suffix(".json").read_text())
    assert report["judge_physical_requests"] == report["judge_request_ceiling"] == 4
    assert report["summary"]["goal_pairs"] == 2
    assert report["summary"]["judge_orders"] == 4


def test_existing_result_cannot_be_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "first"
    original = b"retained first outcome"
    output.with_suffix(".json").write_bytes(original)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launch-report",
            str(tmp_path / "missing.json"),
            "--judgments",
            str(tmp_path / "missing-judgments.json"),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(ValueError, match="preserve the first result"):
        launch_report.main()
    assert output.with_suffix(".json").read_bytes() == original


@pytest.mark.parametrize(
    ("repository", "main_runs", "branch_runs"),
    [("--help", "1,2", "3,4"), ("owner/repo", "1", "3,4"), ("owner/repo", "1,2", "1,4")],
)
def test_download_inputs_refuse_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    main_runs: str,
    branch_runs: str,
) -> None:
    def deny(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("invalid inputs must not make requests")

    monkeypatch.setattr(subprocess, "check_output", deny)
    with pytest.raises(ValueError):
        launch_report.download_manifest(repository, main_runs, branch_runs, tmp_path / "downloads")


@pytest.mark.parametrize("bad_run", [False, True])
def test_downloaded_partial_report_keeps_missing_slots_and_checks_run_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_run: bool,
) -> None:
    source = manifest(tmp_path)
    data = json.loads(source.read_text())
    files = {
        1: tmp_path / data["cell-biology"]["main"],
        2: tmp_path / data["battery-materials"]["main"],
    }
    routes: list[str] = []

    def check_output(command: list[str], *, timeout: int) -> bytes:
        route = command[2]
        routes.append(route)
        assert route.startswith("repos/current-owner/research/actions/runs/")
        run_id = int(route.split("/")[5])
        if route.endswith("/artifacts"):
            return json.dumps(
                {
                    "artifacts": [
                        {
                            "id": run_id,
                            "name": "benchmark-express-fake",
                            "expired": False,
                            "size_in_bytes": files[run_id].stat().st_size,
                        }
                    ]
                }
            ).encode()
        return json.dumps(
            {
                "path": ".github/workflows/benchmark.yml",
                "event": "push" if bad_run else "workflow_dispatch",
                "status": "completed",
            }
        ).encode()

    def run(
        command: list[str], *, stdout: BinaryIO, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        route = command[2]
        routes.append(route)
        assert route.startswith("repos/current-owner/research/actions/artifacts/")
        stdout.write(files[int(route.split("/")[5])].read_bytes())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "run", run)
    directory = tmp_path / "downloads"
    if bad_run:
        with pytest.raises(ValueError, match="completed manual"):
            launch_report.download_manifest(
                "current-owner/research", "1,2", "not-run,not-run", directory
            )
        assert len(routes) == 1
        return
    downloaded = launch_report.download_manifest(
        "current-owner/research", "1,2", "not-run,not-run", directory
    )
    prepared = launch_report.prepare(downloaded, tmp_path / "prepared")
    assert len(routes) == 6
    assert all(set(arms) == {"main"} for arms in prepared.values())
    assert launch_report.report(prepared, lambda packet: {})["summary"]["goal_pairs"] == 0
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.glob("*.zip"))
