from __future__ import annotations

import argparse
import json
import math
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from evaluations._artifacts import build_provenance
from evaluations._paired_db import Snapshot, read_snapshot
from evaluations._paired_judge import Judge, judge_pair, live_judge, packets, recorded_judge
from evaluations.quality_goals import GOAL_VERSION, GOALS

_DELTAS = (
    "ideas_delivered",
    "ideas_featured",
    "ideas_screened",
    "supported_claims",
    "delivered_supported_claims",
    "wall_seconds",
    "physical_calls",
    "total_tokens",
)


def _validate_pair(main: Snapshot, branch: Snapshot) -> None:
    for field in ("tier", "backend"):
        if main.metrics[field] != branch.metrics[field]:
            raise ValueError(f"paired {field} differs")
    for field in ("configured_models", "tools", "execution_environment"):
        if field not in main.identity or field not in branch.identity:
            raise ValueError(f"paired control {field} is missing")
        if main.identity.get(field) != branch.identity.get(field):
            raise ValueError(f"paired control {field} differs; rerun both arms")


def compare(main: Snapshot, branch: Snapshot, judge: Judge) -> dict[str, Any]:
    _validate_pair(main, branch)
    judgment = judge_pair(GOALS[main.goal_id], main.report, branch.report, judge)
    delta: dict[str, Any] = {
        field: branch.metrics[field] - main.metrics[field]
        if main.metrics[field] is not None and branch.metrics[field] is not None
        else None
        for field in _DELTAS
    }
    concerns = []
    if not main.metrics["completed"]:
        concerns.append("baseline incomplete; comparison invalid")
    if not branch.metrics["completed"]:
        concerns.append("branch incomplete")
    if delta["ideas_delivered"] < 0:
        concerns.append("fewer report ideas delivered")
    if delta["delivered_supported_claims"] < 0:
        concerns.append("fewer supported claims delivered; inspect evidence")
    if judgment["winner"] == "main":
        concerns.append("both report orders prefer main")
    if any(delta[field] is None for field in ("wall_seconds", "physical_calls", "total_tokens")):
        concerns.append("efficiency telemetry incomplete")
    if main.metrics["backend"] != "real":
        concerns.append("offline wiring only; not scientific quality evidence")
    return {
        "goal_id": main.goal_id,
        "main": main.metrics,
        "branch": branch.metrics,
        "controls": {"main": main.identity, "branch": branch.identity},
        "delta_branch_minus_main": delta,
        "judgment": judgment,
        "concerns": concerns,
    }


def _sign_test(wins: int, losses: int) -> float | None:
    n = wins + losses
    if not n:
        return None
    tail: float = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    winners = [row["judgment"]["winner"] for row in rows]
    wins, losses = winners.count("branch"), winners.count("main")
    concerns = [f"{row['goal_id']}: {c}" for row in rows for c in row["concerns"]]
    return {
        "goal_pairs": len(rows),
        "research_runs": 2 * len(rows),
        "judge_orders": sum(len(row["judgment"]["orders"]) for row in rows),
        "branch_wins": wins,
        "main_wins": losses,
        "ties": winners.count("tie"),
        "missing_judgments": winners.count(None),
        "position_disagreements": sum(
            row["judgment"].get("order_disagreement", False) for row in rows
        ),
        "two_sided_sign_test_p": _sign_test(wins, losses),
        "decision": "review_required" if concerns else "inconclusive",
        "concerns": concerns,
        "noise": (
            "Differences are inside the noise of this three-goal sample. Even three consistent "
            "wins give two-sided sign-test p=0.25. Two report orders are one paired judgment, "
            "not two independent samples. No small quality loss or equivalence is established. "
            "Completion and delivery concerns still require review. Claims cluster within ideas; "
            "supported counts and verdict mixes are descriptive, never an unsupported-rate gate."
        ),
    }


def load_pairs(manifest: Path) -> list[tuple[Snapshot, Snapshot]]:
    data = json.loads(manifest.read_text())
    entries = data["pairs"]
    if sorted(e["goal_id"] for e in entries) != sorted(GOALS):
        raise ValueError("manifest must contain exactly one pair for each fixed goal")
    for arm in ("main", "branch"):
        if len({entry[arm]["source_commit"] for entry in entries}) != 1:
            raise ValueError(f"all {arm} goals must use the same source commit")
    pairs = []
    for entry in entries:
        arms = []
        for arm in ("main", "branch"):
            ref = entry[arm]
            arms.append(
                read_snapshot(
                    manifest.parent / ref["db"],
                    ref["run_id"],
                    entry["goal_id"],
                    ref["source_commit"],
                )
            )
        _validate_pair(*arms)
        pairs.append((arms[0], arms[1]))
    return pairs


def _cell(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, dict):
        return ", ".join(f"{k}:{v}" for k, v in sorted(value.items())) or "none"
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "| Goal | Arm | Complete | Ideas (featured / screened) | Verification mix | "
        "Supported claims "
        "(delivered / all; assessed) | Wall s | Calls | Tokens | Blind judgment |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["pairs"]:
        judgment = row["judgment"]["winner"]
        if row["judgment"].get("order_disagreement"):
            judgment = "tie (orders disagree)"
        for arm in ("main", "branch"):
            m = row[arm]
            cells = [
                row["goal_id"],
                arm,
                m["completed"],
                f"{m['ideas_delivered']} ({m['ideas_featured']} / {m['ideas_screened']})",
                m["verification_verdict_mix"],
                f"{m['delivered_supported_claims']} / {m['supported_claims']}; "
                f"n={m['claims_assessed']}",
                m["wall_seconds"],
                m["physical_calls"],
                m["total_tokens"],
                judgment,
            ]
            lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    summary = report["summary"]
    lines += [
        "",
        f"n={summary['goal_pairs']} goal pairs, {summary['research_runs']} research runs, "
        f"{summary['judge_orders']} judge orders. Decision: {summary['decision']}.",
        "",
        summary["noise"],
    ]
    if summary["concerns"]:
        lines += ["", *[f"- {c}" for c in summary["concerns"]]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Paired quality check over three fixed goals.")
    parser.add_argument("manifest", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--judgments", type=Path, help="Recorded judgments keyed by packet SHA256.")
    group.add_argument("--export-packets", action="store_true")
    group.add_argument(
        "--live-judge", action="store_true", help="Explicit free-route model requests."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-judge-calls", type=int, default=6)
    args = parser.parse_args()
    if args.live_judge:
        if not 1 <= args.max_judge_calls <= 150:
            parser.error("--max-judge-calls must be between 1 and 150")
        if (
            os.getenv("COSCIENTIST_TEST_DOUBLE") == "deterministic"
            or os.getenv("COSCIENTIST_FORCE_OFFLINE") == "1"
        ):
            raise ValueError("live judge cannot use the deterministic test adapter")
        from evaluations._live_config import configure_live_environment

        model = configure_live_environment()
    else:
        model = None
    pairs = load_pairs(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.export_packets:
        from evaluations._identity import identity_digest

        exported = {
            identity_digest(p): p
            for a, b in pairs
            for p in packets(GOALS[a.goal_id], a.report, b.report)
        }
        args.output.write_text(json.dumps(exported, indent=2) + "\n")
        return 0
    judge = live_judge(model) if model else recorded_judge(json.loads(args.judgments.read_text()))
    from evaluations._usage_evidence import capture_usage

    bounded_backend = None
    with ExitStack() as stack:
        if args.live_judge:
            from co_scientist.platform.llm import scoped_completion_budget
            from co_scientist.platform.llm.request.backend import active_backend, using_backend

            from evaluations.quality_benchmark import BoundedBackend

            stack.enter_context(scoped_completion_budget(args.max_judge_calls))
            bounded_backend = BoundedBackend(active_backend(), args.max_judge_calls)
            stack.enter_context(using_backend(bounded_backend))
        evidence = stack.enter_context(capture_usage("paired_quality_judge", live=args.live_judge))
        rows = [compare(a, b, judge) for a, b in pairs]
    report: dict[str, Any] = {
        "goal_version": GOAL_VERSION,
        "judge_mode": "live" if args.live_judge else "recorded",
        "judge_model": model,
        "judge_request_ceiling": args.max_judge_calls if args.live_judge else None,
        "judge_physical_requests": bounded_backend.calls if bounded_backend else 0,
        "judge_usage": evidence,
        "pairs": rows,
        "summary": summarize(rows),
        "provenance": build_provenance(model=model),
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    table = markdown(report)
    args.output.with_suffix(".md").write_text(table)
    print(table, end="")
    return 2 if report["summary"]["decision"] == "review_required" else 0


if __name__ == "__main__":
    raise SystemExit(main())
