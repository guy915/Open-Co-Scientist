from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from evaluations import benchmark_transport, paired_artifacts, paired_quality
from evaluations._artifacts import build_provenance
from evaluations._paired_db import Snapshot, read_snapshot
from evaluations._paired_judge import Judge, live_judge, recorded_judge
from evaluations.benchmark_artifact_safety import check_artifacts

GOALS = ("cell-biology", "battery-materials")


def download_manifest(repository: str, main_runs: str, branch_runs: str, directory: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    slots = {"main": main_runs.split(","), "branch": branch_runs.split(",")}
    if any(
        len(ids) != 2 or any(i != "not-run" and not re.fullmatch(r"[1-9][0-9]*", i) for i in ids)
        for ids in slots.values()
    ):
        raise ValueError("provide biology,battery workflow IDs; not-run means a missing slot")
    ids = [i for values in slots.values() for i in values if i != "not-run"]
    if len(set(ids)) != len(ids):
        raise ValueError("report workflow run IDs must be distinct")
    directory.mkdir(mode=0o700, exist_ok=False)
    manifest: dict[str, dict[str, str | None]] = {goal: {} for goal in GOALS}
    for arm, values in slots.items():
        for goal, run_id in zip(GOALS, values, strict=True):
            manifest[goal][arm] = None
            if run_id == "not-run":
                continue
            route = f"repos/{repository}/actions/runs/{run_id}"
            run = json.loads(subprocess.check_output(["gh", "api", route], timeout=60))
            if (
                run["path"] != ".github/workflows/benchmark.yml"
                or run["event"] != "workflow_dispatch"
                or run["status"] != "completed"
            ):
                raise ValueError("report needs completed manual Benchmark dispatches")
            artifacts = json.loads(
                subprocess.check_output(["gh", "api", f"{route}/artifacts"], timeout=60)
            )["artifacts"]
            candidates = [
                a
                for a in artifacts
                if a["name"].startswith("benchmark-express-") and not a["expired"]
            ]
            if (
                len(candidates) != 1
                or candidates[0]["size_in_bytes"] > paired_artifacts._MAX_ARCHIVE_BYTES
            ):
                raise ValueError("report run needs one bounded Express archive")
            archive = directory / f"{goal}-{arm}.zip"
            with archive.open("xb") as dest:
                archive.chmod(0o600)
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
            manifest[goal][arm] = archive.name
    path = directory / "runs.json"
    path.write_text(json.dumps(manifest))
    return path


def prepare(
    manifest: Path, directory: Path, credentials: list[str] | None = None
) -> dict[str, dict[str, Snapshot]]:
    data = json.loads(manifest.read_text())
    if set(data) != set(GOALS):
        raise ValueError("launch report requires exactly biology and battery slots")
    directory.mkdir(exist_ok=False)
    snapshots: dict[str, dict[str, Snapshot]] = {}
    credentials = (
        credentials
        if credentials is not None
        else [
            value
            for name, value in os.environ.items()
            if name.upper().endswith("_API_KEY") or name == "COSCIENTIST_MCP_SHARED_SECRET"
        ]
    )
    for goal in GOALS:
        if set(data[goal]) != {"main", "branch"}:
            raise ValueError("each goal needs explicit main and branch slots; null means not run")
        snapshots[goal] = {}
        for arm, archive in data[goal].items():
            if archive is None:
                continue
            target = directory / f"{goal}-{arm}"
            target.mkdir()
            receipt = paired_artifacts._extract(manifest.parent / archive, target)
            check_artifacts([target], credentials)
            ref = paired_artifacts._validate_receipt(receipt, target / "snapshot.db")
            portable = target / "portable.db"
            portable.write_bytes((target / "snapshot.db").read_bytes())
            paired_artifacts._validate_receipt(receipt, portable)
            if ref["goal_id"] != goal:
                raise ValueError("archive goal does not match its report slot")
            snapshots[goal][arm] = read_snapshot(
                target / "snapshot.db", ref["run_id"], goal, ref["source_commit"]
            )
    for arm in ("main", "branch"):
        sources = {arms[arm].metrics["source_commit"] for arms in snapshots.values() if arm in arms}
        if len(sources) > 1:
            raise ValueError("all goals in an arm must use one immutable source")
    observed = [snapshot for arms in snapshots.values() for snapshot in arms.values()]
    controls = {
        json.dumps(
            {
                name: snapshot.identity[name]
                for name in ("configured_models", "tools", "execution_environment")
            },
            sort_keys=True,
        )
        for snapshot in observed
    }
    counters = {snapshot.metrics["request_counter_sha256"] for snapshot in observed}
    if len(controls) > 1 or len(counters) > 1:
        raise ValueError("launch report provider/tool/environment/HTTP controls differ")
    return snapshots


def report(snapshots: dict[str, dict[str, Snapshot]], judge: Judge) -> dict[str, Any]:
    rows = []
    for goal in GOALS:
        arms = snapshots[goal]
        if all(arm in arms and arms[arm].metrics["completed"] for arm in ("main", "branch")):
            rows.append(paired_quality.compare(arms["main"], arms["branch"], judge))
    summary = paired_quality.summarize(rows)
    summary["decision"] = "owner_review_report"
    measured = (
        "Statistical quality differences remain inside the noise of this small sample; "
        "even two consistent wins give two-sided sign-test p=0.5. "
        if rows
        else "No paired quality or efficiency difference was measured. "
    )
    summary["noise"] = (
        f"n={len(rows)} complete goal pairs. {measured}Swapped report "
        "orders are one paired judgment, not independent samples. No equivalence or small "
        "quality loss is established. Completion/delivery concerns require review. "
        "Hydrology is outside this two-goal report; unfinished slots are not run."
    )
    return {
        "requested_goals": list(GOALS),
        "dropped_goals": {"urban-hydrology": "not run"},
        "runs": {
            goal: {arm: snapshot.metrics for arm, snapshot in arms.items()}
            for goal, arms in snapshots.items()
        },
        "controls": {
            goal: {arm: snapshot.identity for arm, snapshot in arms.items()}
            for goal, arms in snapshots.items()
        },
        "pairs": rows,
        "summary": summary,
    }


def markdown(result: dict[str, Any]) -> str:
    lines = paired_quality.markdown(result).splitlines()
    paired = {row["goal_id"] for row in result["pairs"]}
    extra = []
    for goal in (*GOALS, "urban-hydrology"):
        if goal not in paired:
            for arm in ("main", "branch"):
                metrics = result["runs"].get(goal, {}).get(arm)
                if metrics and metrics["completed"]:
                    cells = [
                        goal,
                        arm,
                        True,
                        f"{metrics['ideas_delivered']} ({metrics['ideas_featured']} / "
                        f"{metrics['ideas_screened']})",
                        metrics["verification_verdict_mix"],
                        f"{metrics['delivered_supported_claims']} / "
                        f"{metrics['supported_claims']}; n={metrics['claims_assessed']}",
                        metrics["wall_seconds"],
                        metrics["physical_calls"],
                        metrics["total_tokens"],
                        "not run",
                    ]
                    extra.append("| " + " | ".join(paired_quality._cell(c) for c in cells) + " |")
                else:
                    extra.append(
                        f"| {goal} | {arm} | not run | unknown | unknown | unknown | "
                        "unknown | unknown | unknown | not run |"
                    )
    boundary = next(index for index, line in enumerate(lines) if not line)
    lines[boundary:boundary] = extra
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Two-goal paired quality report.")
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--main-runs", default="")
    parser.add_argument("--branch-runs", default="")
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--judgments", type=Path)
    mode.add_argument("--live-judge", action="store_true")
    args = parser.parse_args()
    if bool(args.manifest) == args.download:
        raise ValueError("provide a recorded manifest or explicit download mode")
    if any(args.output.with_suffix(suffix).exists() for suffix in (".json", ".md")):
        raise ValueError("launch report requires a new output prefix; preserve the first result")
    credentials = [
        value
        for name, value in os.environ.items()
        if name.upper().endswith("_API_KEY") or name == "COSCIENTIST_MCP_SHARED_SECRET"
    ]
    model = None
    if args.live_judge:
        if os.getenv("COSCIENTIST_TEST_DOUBLE") or os.getenv("COSCIENTIST_FORCE_OFFLINE") == "1":
            raise ValueError("live launch report refuses deterministic execution")
        from evaluations._live_config import configure_live_environment

        model = configure_live_environment()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    staging = args.output.parent / (args.output.name + "-private")
    staging.mkdir(mode=0o700, exist_ok=False)
    manifest = (
        download_manifest(args.repository, args.main_runs, args.branch_runs, staging / "downloads")
        if args.download
        else args.manifest
    )
    snapshots = prepare(manifest, staging / "inputs", credentials)
    observed = [snapshot for arms in snapshots.values() for snapshot in arms.values()]
    if model and any(
        s.metrics["request_counter_sha256"] != benchmark_transport.COUNTER_SHA256 for s in observed
    ):
        raise ValueError("judge HTTP/SDK counter differs from the collected runs")
    judge = live_judge(model) if model else recorded_judge(json.loads(args.judgments.read_text()))
    bounded = None
    with ExitStack() as stack:
        if model:
            from co_scientist.platform.llm import scoped_completion_budget
            from co_scientist.platform.llm.request.backend import active_backend, using_backend

            from evaluations import quality_benchmark

            stack.enter_context(benchmark_transport.count_http_attempts())
            stack.enter_context(scoped_completion_budget(4))
            bounded = quality_benchmark.BoundedBackend(active_backend(), 4)
            stack.enter_context(using_backend(bounded))
        from evaluations._usage_evidence import capture_usage

        usage = stack.enter_context(capture_usage("launch_report_judge", live=args.live_judge))
        result = report(snapshots, judge)
    result.update(
        judge_model=model,
        judge_mode="live" if model else "recorded",
        judge_request_ceiling=4 if model else None,
        judge_physical_requests=bounded.calls if bounded else 0,
        judge_usage=usage,
        provenance=build_provenance(model=model),
    )
    json_file, markdown_file = staging / "report.json", staging / "report.md"
    json_file.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    markdown_file.write_text(markdown(result))
    check_artifacts([json_file, markdown_file], credentials)
    json_file.replace(args.output.with_suffix(".json"))
    markdown_file.replace(args.output.with_suffix(".md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
