"""How much of what a run asserts is backed by something it retrieved.

The unsupported-claim rate is the blunt end of the deep-research work: if
going back for what a first search left open is worth its cost, this is
the number it has to move. It is deliberately computed from verdicts the
run already recorded rather than re-judged here -- ``claim_evidence``
rows are written by the production assessor
(``co_scientist.domains.research_state.claims.verifier``), and
a second, lexical opinion invented inside an eval is how a metric comes
to disagree with the product it is measuring. That failure has a name in
this repo: the Jaccard incident, where a short claim scored against a
long abstract made the two upper verdict states unreachable and every
citation in every run classified ``unsupported``.

"Supported" therefore means exactly what the report means by it -- a
``supports`` or ``partial`` claim-evidence edge -- reused from
``_run_driver`` rather than restated.

Two rates, because they answer different questions:

- **unsupported_claim_rate** -- over every assessed claim. The direct
  measure, and the one a retrieval change should move.
- **unverified_idea_rate** -- over hypotheses. An idea whose every claim
  went unsupported is what a reader actually sees badged "Unverified", so
  a change that helps already-well-supported ideas and no others shows up
  here as no movement.

**Offline by default, and that proves wiring, not quality.** The
deterministic offline backend answers every assessment the same canned
way whatever was retrieved, so an offline number says the path computes;
it says nothing about whether research improved support. Offline
artifacts carry an ``offline_disclaimer`` saying so. ``--run <id>``
scores a real persisted run, which is the mode that means something.
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import sqlite3
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT / "app") not in sys.path:
    sys.path.insert(0, str(_ROOT / "app"))

from evaluations._artifacts import write_dated_artifact  # noqa: E402
from evaluations._run_driver import (  # noqa: E402
    ArmInvocation,
    configure_environment,
    hypotheses_with_claim_counts,
    run_arm,
)

_OFFLINE_DISCLAIMER = (
    "Offline run: the deterministic backend answers every claim assessment "
    "identically regardless of what was retrieved, so this rate proves the "
    "measurement path and not that research improved support."
)

_GOAL = (
    "Propose a mechanism for acquired resistance to EGFR tyrosine kinase "
    "inhibitors in EGFR-mutant lung adenocarcinoma."
)


def score_claims(annotated: list[dict[str, Any]]) -> dict[str, Any]:
    """No assessed claims means an unknown rate, not perfect support."""
    assessed = sum(int(h.get("assessed_claims") or 0) for h in annotated)
    supported = sum(int(h.get("verified_claims") or 0) for h in annotated)
    with_claims = [h for h in annotated if int(h.get("assessed_claims") or 0)]
    unverified = [h for h in with_claims if not int(h.get("verified_claims") or 0)]
    return {
        "ideas": len(annotated),
        "ideas_with_assessed_claims": len(with_claims),
        "claims_assessed": assessed,
        "claims_supported": supported,
        "unsupported_claim_rate": _rate(assessed - supported, assessed),
        "unsupported_claim_rate_ci95": _wilson_interval(assessed - supported, assessed),
        "unverified_idea_rate": _rate(len(unverified), len(with_claims)),
    }


def _rate(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def _wilson_interval(part: int, total: int, z: float = 1.96) -> list[float] | None:
    """Claims cluster within ideas, so the true run-to-run spread is wider still."""
    if not total:
        return None
    p = part / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    scale = 1 + z * z / total
    return [
        round(max(0.0, (centre - margin) / scale), 4),
        round(min(1.0, (centre + margin) / scale), 4),
    ]


def score_run(run_id: str, db_path: str | None = None) -> dict[str, Any]:
    from co_scientist.domains.research_state.repository import hypotheses as store
    from co_scientist.domains.research_state.repository import records
    from co_scientist.platform.db import retrieval_calls as retrieval
    from co_scientist.platform.db import runs

    from evaluations._usage_evidence import summarize_usage

    metrics = retrieval.get_run_metrics(run_id, db_path=db_path) or {}
    run = runs.get_run(run_id, db_path=db_path)
    hyps = store.list_hypotheses(run_id, db_path=db_path)
    edges = records.list_claim_evidence(run_id, db_path=db_path)
    return {
        "run_id": run_id,
        "configured_backend": run.llm_backend if run is not None else None,
        "evaluation_identity": (run.config.get("evaluation_identity") if run is not None else None),
        "usage_evidence": summarize_usage(metrics.get("model_usage") or {}),
        **score_claims(hypotheses_with_claim_counts(hyps, edges)),
    }


# Benchmarks set this to keep the run database for offline analysis; the
# temporary store is otherwise deleted with its directory.
KEEP_DB_ENV = "CLAIM_SUPPORT_KEEP_DB"


def stage_timings(db_path: str, run_id: str) -> dict[str, Any]:
    """Busy time sums concurrent tasks, so per-stage seconds can exceed the
    run's wall time; compare stages with each other, not with the wall.
    """
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT task_type, status, attempt, started_at, completed_at, available_at"
            " FROM scientific_tasks WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    stages: dict[str, dict[str, Any]] = {}
    starts: list[float] = []
    ends: list[float] = []
    for task_type, status, attempt, started, completed, available in rows:
        stage = stages.setdefault(
            task_type,
            {"tasks": 0, "failed": 0, "attempts": 0, "parked": 0, "busy_s": 0.0, "max_s": 0.0},
        )
        stage["tasks"] += 1
        stage["failed"] += status == "failed"
        stage["attempts"] += attempt or 0
        stage["parked"] += available is not None
        if started is not None:
            starts.append(started)
        if completed is not None:
            ends.append(completed)
        if started is not None and completed is not None:
            seconds = completed - started
            stage["busy_s"] += seconds
            stage["max_s"] = max(stage["max_s"], seconds)
    for stage in stages.values():
        stage["busy_s"] = round(stage["busy_s"], 1)
        stage["max_s"] = round(stage["max_s"], 1)
    return {
        "wall_s": round(max(ends) - min(starts), 1) if starts and ends else None,
        "stages": dict(sorted(stages.items(), key=lambda item: -item[1]["busy_s"])),
    }


def _keep_database(db_path: str) -> None:
    target = os.environ.get(KEEP_DB_ENV)
    if not target:
        return
    pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)
    # The backup API copies a consistent snapshot, including WAL contents.
    with sqlite3.connect(db_path) as source, sqlite3.connect(target) as copy:
        source.backup(copy)


def drive_and_score(
    goal: str = _GOAL, tier: str = "express", *, live: bool = False
) -> dict[str, Any]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(pathlib.Path(tmp) / "claims.db")
        configure_environment(db_path, live=live)
        arm = run_arm(
            goal,
            tier,
            {},
            ArmInvocation(
                client_id="claim-support-eval",
                backend="real" if live else "offline",
                db_path=db_path,
            ),
        )
        timings = stage_timings(db_path, arm["run_id"])
        _keep_database(db_path)
    return {
        "run_id": arm["run_id"],
        "tier": tier,
        "completed": arm["completed"],
        "used_real_backend": arm["used_real_backend"],
        "evaluation_identity": arm.get("evaluation_identity"),
        "llm_calls": arm["metrics"]["llm_calls"],
        "latency_seconds": arm["metrics"]["latency_seconds"],
        "usage_evidence": arm["metrics"]["usage_evidence"],
        "stage_timings": timings,
        **score_claims(arm["hypotheses"]),
    }


def run(run_id: str | None, *, live: bool = False, tier: str = "express") -> dict[str, Any]:
    if run_id:
        return {
            "mode": "persisted_run",
            "offline_disclaimer": None,
            **score_run(run_id),
        }
    return {
        "mode": "live" if live else "offline",
        "offline_disclaimer": None if live else _OFFLINE_DISCLAIMER,
        "goal": _GOAL,
        **drive_and_score(tier=tier, live=live),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default=None, help="Score this run instead.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Drive the run against a real provider (needs a key).",
    )
    parser.add_argument(
        "--tier",
        default="express",
        choices=("express", "standard", "extended", "ultra"),
        help="Run size for a driven run.",
    )
    args = parser.parse_args()

    report = run(args.run, live=args.live, tier=args.tier)
    out = write_dated_artifact(report, f"claim-support-{report['mode']}")
    print(
        f"claim support [{report['mode']}]: "
        f"claims={report['claims_assessed']} "
        f"unsupported_claim_rate={report['unsupported_claim_rate']} "
        f"(n={report['claims_assessed']}, 95% CI {report['unsupported_claim_rate_ci95']}) "
        f"unverified_idea_rate={report['unverified_idea_rate']}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
