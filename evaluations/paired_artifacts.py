from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from evaluations._paired_db import read_snapshot
from evaluations.paired_quality import load_pairs
from evaluations.quality_goals import GOAL_VERSION, GOALS

_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MEMBERS = {
    "receipt.json": 1024 * 1024,
    "snapshot.db": 64 * 1024 * 1024,
    "snapshot.db-wal": 64 * 1024 * 1024,
}


def _extract(archive: Path, output: Path) -> dict[str, Any]:
    if archive.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("benchmark archive is too large")
    with zipfile.ZipFile(archive) as bundle:
        for name, limit in _MEMBERS.items():
            members = [
                m for m in bundle.infolist() if m.filename in {name, f"benchmark-artifacts/{name}"}
            ]
            if name.endswith("-wal") and not members:
                continue
            if len(members) != 1 or members[0].file_size > limit:
                raise ValueError(f"benchmark archive needs one bounded {name}")
            # Only these two members are read, then written under fixed local names.
            (output / name).write_bytes(bundle.read(members[0]))
    receipt: dict[str, Any] = json.loads((output / "receipt.json").read_text())
    return receipt


def _validate_receipt(
    receipt: dict[str, Any], db: Path, *, provider: str = "free"
) -> dict[str, str]:
    goal_id = receipt["goal_id"]
    if receipt["goal_version"] != GOAL_VERSION or receipt["goal"] != GOALS[goal_id]:
        raise ValueError("benchmark goal/version differs from the fixed cohort")
    count, ceiling = receipt["physical_requests_attempted"], receipt["physical_request_ceiling"]
    if type(count) is not int or type(ceiling) is not int or not 0 <= count <= ceiling <= 450:
        raise ValueError("invalid physical request count or ceiling")
    source, run_id = receipt["source_commit"], receipt["metrics"]["run_id"]
    snapshot = read_snapshot(db, run_id, goal_id, source)
    if (
        receipt["metrics"] != snapshot.metrics
        or receipt["evaluation_identity"] != snapshot.identity
    ):
        raise ValueError("benchmark receipt differs from its database")
    if snapshot.metrics["call_count_basis"] != "http_transport_attempts":
        raise ValueError(
            "benchmark needs a versioned HTTP attempt counter; legacy invocations are insufficient"
        )
    if snapshot.metrics["tier"] != "express" or snapshot.metrics["backend"] != "real":
        raise ValueError("paired dispatch requires live Express artifacts")
    if receipt["offline_disclaimer"] is not None:
        raise ValueError("offline artifacts are wiring evidence only")
    environment = snapshot.identity["execution_environment"]
    if provider == "free":
        if environment.get("COSCIENTIST_REQUIRE_FREE_MODELS") != "1":
            raise ValueError("benchmark did not require free models")
    elif provider in {"anthropic", "azure"}:
        if (
            environment.get("COSCIENTIST_REQUIRE_FREE_MODELS") != "0"
            or environment.get("COSCIENTIST_BENCHMARK_PROVIDER") != provider
            or not all(
                re.fullmatch(r"[a-f0-9]{64}", environment.get(name) or "")
                for name in (
                    "COSCIENTIST_BENCHMARK_POLICY_SHA256",
                    "COSCIENTIST_BENCHMARK_ENDPOINT_SHA256",
                )
            )
            or not all(
                isinstance(model, str) and model.startswith(provider + "/")
                for model in snapshot.identity["configured_models"].values()
            )
            or set(snapshot.identity["configured_models"])
            != {"worker", "supervisor", "chat", "safety", "claim_verifier"}
        ):
            raise ValueError("benchmark paid provider controls are missing or inconsistent")
    else:
        raise ValueError("unknown benchmark provider policy")
    recorded_source = receipt["provenance"]["source"]
    if recorded_source["git_commit"] != source or recorded_source["git_dirty"] is not False:
        raise ValueError("benchmark source provenance is inconsistent")
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT source_commit, physical_requests, request_ceiling FROM evaluation_runs "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if row != (source, count, ceiling):
        raise ValueError("independent dispatch receipt differs")
    return {"goal_id": goal_id, "db": str(db), "run_id": run_id, "source_commit": source}


def prepare(archives: dict[str, list[Path]], output: Path, *, provider: str = "free") -> Path:
    if output.exists():
        raise ValueError("paired preparation requires a new output directory")
    if set(archives) != {"main", "branch"} or any(len(v) != 3 for v in archives.values()):
        raise ValueError("provide three main and three branch archives")
    if len({p.resolve() for paths in archives.values() for p in paths}) != 6:
        raise ValueError("six distinct benchmark archives are required")
    output.mkdir(parents=True)
    arms: dict[str, dict[str, dict[str, str]]] = {}
    for arm, paths in archives.items():
        arms[arm] = {}
        for index, archive in enumerate(paths):
            directory = output / f"{arm}-{index}"
            directory.mkdir()
            receipt = _extract(archive, directory)
            ref = _validate_receipt(receipt, directory / "snapshot.db", provider=provider)
            goal_id = ref.pop("goal_id")
            if goal_id in arms[arm]:
                raise ValueError("duplicate goal in benchmark arm")
            ref["db"] = str(Path(ref["db"]).relative_to(output))
            arms[arm][goal_id] = ref
        if set(arms[arm]) != set(GOALS):
            raise ValueError("benchmark arm does not cover the fixed cohort")
    manifest = output / "pairs.json"
    manifest.write_text(
        json.dumps(
            {
                "pairs": [
                    {"goal_id": g, "main": arms["main"][g], "branch": arms["branch"][g]}
                    for g in GOALS
                ]
            },
            indent=2,
        )
        + "\n"
    )
    pairs = load_pairs(manifest)
    if len({s.metrics["request_counter_sha256"] for pair in pairs for s in pair}) != 1:
        raise ValueError("benchmark HTTP instrumentation differs; rerun both arms")
    if provider != "free":
        controls = {
            json.dumps(
                {
                    name: s.identity[name]
                    for name in ("configured_models", "tools", "execution_environment")
                },
                sort_keys=True,
            )
            for pair in pairs
            for s in pair
        }
        if len(controls) != 1:
            raise ValueError("paid provider controls differ across the six-run cohort")
    return manifest


def parse_run_ids(value: str) -> list[str]:
    ids = value.split(",")
    if len(ids) != 3 or any(not re.fullmatch(r"[1-9][0-9]*", i) for i in ids):
        raise ValueError("provide three comma-separated workflow run IDs")
    return ids


def download(repository: str, runs: dict[str, list[str]], output: Path) -> dict[str, list[Path]]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    if len({i for ids in runs.values() for i in ids}) != 6:
        raise ValueError("six distinct workflow run IDs are required")
    output.mkdir(parents=True, exist_ok=False)
    archives: dict[str, list[Path]] = {}
    for arm, ids in runs.items():
        archives[arm] = []
        for run_id in ids:
            route = f"repos/{repository}/actions/runs/{run_id}"
            run = json.loads(subprocess.check_output(["gh", "api", route], timeout=60))
            if (
                run["path"] != ".github/workflows/benchmark.yml"
                or run["event"] != "workflow_dispatch"
                or run["status"] != "completed"
            ):
                raise ValueError("run must be a completed manual Benchmark dispatch")
            artifacts = json.loads(
                subprocess.check_output(
                    ["gh", "api", f"{route}/artifacts"],
                    timeout=60,
                )
            )["artifacts"]
            candidates = [
                a
                for a in artifacts
                if a["name"].startswith("benchmark-express-") and not a["expired"]
            ]
            if len(candidates) != 1 or candidates[0]["size_in_bytes"] > _MAX_ARCHIVE_BYTES:
                raise ValueError("run needs one bounded Express benchmark artifact")
            archive = output / f"{arm}-{run_id}.zip"
            with archive.open("wb") as dest:
                subprocess.run(
                    [
                        "gh",
                        "api",
                        f"repos/{repository}/actions/artifacts/{candidates[0]['id']}/zip",
                    ],
                    stdout=dest,
                    check=True,
                    timeout=120,
                )
            archives[arm].append(archive)
    return archives


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare validated six-run paired artifacts.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--archives", type=Path, help="Recorded JSON main/branch ZIP paths.")
    mode.add_argument(
        "--download", action="store_true", help="Explicit read-only GitHub downloads."
    )
    parser.add_argument(
        "--repository",
        default=os.getenv("GITHUB_REPOSITORY", ""),
        help="Download repository; defaults to GITHUB_REPOSITORY.",
    )
    parser.add_argument("--main-runs")
    parser.add_argument("--branch-runs")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("free", "anthropic", "azure"), default="free")
    args = parser.parse_args()
    if args.download:
        if not args.main_runs or not args.branch_runs:
            parser.error("--download requires --main-runs and --branch-runs")
        archives = download(
            args.repository,
            {
                "main": parse_run_ids(args.main_runs),
                "branch": parse_run_ids(args.branch_runs),
            },
            args.output / "downloads",
        )
    else:
        data = json.loads(args.archives.read_text())
        archives = {arm: [args.archives.parent / p for p in paths] for arm, paths in data.items()}
    print(prepare(archives, args.output / "prepared", provider=args.provider))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
