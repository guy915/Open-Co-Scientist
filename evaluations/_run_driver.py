from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
import sys
import time
from collections import defaultdict
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT / "app") not in sys.path:
    sys.path.insert(0, str(_ROOT / "app"))

_SUPPORTED_CLAIM_LABELS = ("supports", "partial")


def configure_environment(db_path: str, *, live: bool) -> None:
    """Configure before settings imports."""
    os.environ["COSCIENTIST_DB_PATH"] = db_path
    if not live:
        os.environ["COSCIENTIST_TEST_DOUBLE"] = "deterministic"
        # Clear credentials as well as forcing offline so nonconsulting call
        # sites cannot spend.
        for name in [n for n in os.environ if n.endswith("_API_KEY")]:
            del os.environ[name]
        # Synthetic literature cannot support deterministic claims; disable it
        # to exercise downstream stages.
        os.environ["FORCE_LITERATURE_REVIEW"] = "0"
        return
    os.environ.pop("COSCIENTIST_TEST_DOUBLE", None)
    from evaluations._live_config import configure_live_environment

    configure_live_environment()


@dataclasses.dataclass(frozen=True)
class ArmInvocation:
    client_id: str
    backend: str
    db_path: str


def persist_arm_run(
    goal: str, tier: str, overrides: dict[str, Any], invocation: ArmInvocation
) -> str:
    from co_scientist.core.run_modes import resolved_run_config, setup_config
    from co_scientist.platform.db import runs
    from co_scientist.platform.db.runs import RunCreateOptions

    config = resolved_run_config(
        {
            "setup": setup_config(research_goal=goal, tier=tier),
            "tier": tier,
            **overrides,
        }
    )
    from evaluations._identity import arm_identity

    config["evaluation_identity"] = arm_identity(goal, config, invocation.backend)
    run = runs.create_run(
        goal,
        tier,
        "engine",
        config,
        RunCreateOptions(
            client_id=invocation.client_id,
            llm_backend=invocation.backend,
            db_path=invocation.db_path,
        ),
    )
    return str(run.id)


def drive_arm_run(run_id: str, db_path: str) -> tuple[int, float]:
    """Measure elapsed time here: durable execution does not fill the
    standalone total_time metric.
    """
    from co_scientist.platform.llm.offline.llm import install_offline_router

    from evaluations._identity import validate_stored_arm

    identity = validate_stored_arm(run_id, db_path)

    # Direct cohort driving bypasses lifespan, so explicitly install the offline
    # router.
    install_offline_router()
    result = drain_run(run_id, db_path, worker_prefix="eval-driver")
    validate_stored_arm(run_id, db_path, identity)
    return result


def drain_run(run_id: str, db_path: str, *, worker_prefix: str) -> tuple[int, float]:
    """A failed run also drains its queue; verify persisted terminal status."""
    from co_scientist.orchestration import task_worker
    from co_scientist.orchestration.repository import events as store

    task_worker.enqueue_run_workflow(run_id, db_path=db_path)
    start = time.monotonic()
    asyncio.run(
        task_worker.run_run_worker_pool(
            run_id,
            f"{worker_prefix}:{run_id[:8]}",
            policy=task_worker.WorkerPolicy(db_path=db_path),
        )
    )
    elapsed = time.monotonic() - start
    return len(store.list_events(run_id, db_path=db_path)), elapsed


def _claim_counts_by_hypothesis(
    claim_edges: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Verified claims follow the report's supports/partial edge rule."""
    assessed: dict[str, int] = defaultdict(int)
    verified: dict[str, int] = defaultdict(int)
    for edge in claim_edges:
        hid = str(edge.get("hypothesis_id") or "")
        assessed[hid] += 1
        if edge.get("label") in _SUPPORTED_CLAIM_LABELS:
            verified[hid] += 1
    return dict(assessed), dict(verified)


def hypotheses_with_claim_counts(
    hyps: list[dict[str, Any]], claim_edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Store order is Elo-descending; creation_iteration carries the
    authoring timeline.
    """
    assessed, verified = _claim_counts_by_hypothesis(claim_edges)
    out = []
    for h in hyps:
        hid = str(h.get("id") or "")
        out.append(
            {
                "id": hid,
                "text": str(h.get("statement") or h.get("text") or ""),
                "elo_rating": h.get("elo_rating"),
                "creation_iteration": h.get("creation_iteration"),
                "generation": h.get("generation"),
                "created_at": h.get("created_at"),
                "assessed_claims": assessed.get(hid, 0),
                "verified_claims": verified.get(hid, 0),
            }
        )
    return out


def run_completion_status(run: Any) -> tuple[bool, bool]:
    """Queue drain proves neither completion nor a real backend; inspect both
    persisted facts.
    """
    from co_scientist.platform.db import runs as store
    from co_scientist.platform.db.models import RunStatus

    if run is None:
        return False, False
    completed = run.status == RunStatus.COMPLETED.value
    return completed, not store.run_used_offline(run)


def compute_arm_metrics(
    run_id: str, db_path: str, wall_clock_seconds: float, tasks_count: int
) -> dict[str, Any]:
    from co_scientist.platform.db import retrieval_calls as store

    metrics = store.get_run_metrics(run_id, db_path=db_path) or {}
    model_usage = metrics.get("model_usage") or {}
    from evaluations._usage_evidence import summarize_usage

    evidence = summarize_usage(model_usage)
    return {
        "llm_calls": int(metrics.get("llm_calls", 0)),
        "tasks": tasks_count,
        "cost_usd": evidence["partial_estimated_total_usd"],
        "cost_basis": "partial_static_estimate",
        "usage_evidence": evidence,
        "latency_seconds": round(wall_clock_seconds, 3),
    }


def run_arm(
    goal: str,
    tier: str,
    overrides: dict[str, Any],
    invocation: ArmInvocation,
) -> dict[str, Any]:
    from co_scientist.domains.research_state.repository import hypotheses as store
    from co_scientist.domains.research_state.repository import records
    from co_scientist.orchestration.repository import tasks
    from co_scientist.platform.db import runs as store_runs

    db_path = invocation.db_path
    run_id = persist_arm_run(goal, tier, overrides, invocation)
    events, elapsed = drive_arm_run(run_id, db_path)
    run: Any = store_runs.get_run(run_id, db_path=db_path)
    completed, real_backend = run_completion_status(run)
    hyps = store.list_hypotheses(run_id, db_path=db_path)
    claim_edges = records.list_claim_evidence(run_id, db_path=db_path)
    tasks_count = len(tasks.list_tasks(run_id, db_path=db_path))
    return {
        "run_id": run_id,
        "goal": goal,
        "evaluation_identity": run.config["evaluation_identity"],
        "tier": tier,
        "overrides": overrides,
        "completed": completed,
        "used_real_backend": real_backend,
        "events": events,
        "hypotheses": hypotheses_with_claim_counts(hyps, claim_edges),
        "metrics": compute_arm_metrics(run_id, db_path, elapsed, tasks_count),
    }
