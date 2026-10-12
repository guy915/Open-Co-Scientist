from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evaluations._artifacts import build_provenance
from evaluations._paired_db import read_snapshot
from evaluations.quality_goals import GOAL_VERSION, GOALS

if TYPE_CHECKING:
    from co_scientist.platform.llm.request.backend import CompletionBackend


@dataclass
class BoundedBackend:
    delegate: CompletionBackend
    ceiling: int
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def supports_json_schema(self, model_name: str) -> bool:
        return self.delegate.supports_json_schema(model_name)

    async def complete(self, **completion_args: Any) -> Any:
        from co_scientist.core.exceptions import LLMCallBudgetExceededError

        with self._lock:
            if self.calls >= self.ceiling:
                raise LLMCallBudgetExceededError(self.calls + 1, self.ceiling)
            self.calls += 1
        # SDK retries would spend uncounted requests inside one backend invocation.
        completion_args.update(num_retries=0, max_retries=0)
        return await self.delegate.complete(**completion_args)


def collect(goal_id: str, output: Path, *, live: bool, ceiling: int, tier: str) -> dict[str, Any]:
    from evaluations._run_driver import ArmInvocation, configure_environment

    if not 1 <= ceiling <= 450:
        raise ValueError("collection ceiling must be between 1 and 450 physical requests")
    output.mkdir(parents=True, exist_ok=True)
    db = output / "run.db"
    if db.exists():
        raise ValueError(
            "collection requires a new output directory; do not overwrite run databases"
        )
    configure_environment(str(db.resolve()), live=live)
    provenance = build_provenance()
    source = provenance["source"]
    if live and (source["git_dirty"] is not False or source["git_commit"] == "unknown"):
        raise ValueError("live collection requires a clean checkout with a resolved source commit")

    from co_scientist.platform.llm.offline.llm import install_offline_router
    from co_scientist.platform.llm.request.backend import active_backend, using_backend

    from evaluations._identity import validate_stored_arm
    from evaluations._run_driver import drain_run, persist_arm_run

    run_id = persist_arm_run(
        GOALS[goal_id],
        tier,
        {},
        ArmInvocation(
            client_id="paired-quality-benchmark",
            backend="real" if live else "offline",
            db_path=str(db),
        ),
    )
    expected = validate_stored_arm(run_id, str(db))
    install_offline_router()
    backend = BoundedBackend(active_backend(), ceiling)
    error_type = None
    try:
        with using_backend(backend):
            drain_run(run_id, str(db), worker_prefix="quality-benchmark")
        validate_stored_arm(run_id, str(db), expected)
    except Exception as error:
        error_type = type(error).__name__
    finally:
        # Backup includes WAL contents and can be analyzed without the serving process.
        with (
            sqlite3.connect(db) as src,
            sqlite3.connect(output / "snapshot.db") as dest,
        ):
            src.backup(dest)
            dest.execute(
                "CREATE TABLE evaluation_runs (run_id TEXT PRIMARY KEY, source_commit TEXT, "
                "physical_requests INTEGER, request_ceiling INTEGER)"
            )
            dest.execute(
                "INSERT INTO evaluation_runs VALUES (?,?,?,?)",
                (run_id, source["git_commit"], backend.calls, ceiling),
            )
    snapshot = read_snapshot(output / "snapshot.db", run_id, goal_id, source["git_commit"])
    receipt = {
        "goal_version": GOAL_VERSION,
        "goal_id": goal_id,
        "goal": GOALS[goal_id],
        "source_commit": source["git_commit"],
        "physical_request_ceiling": ceiling,
        "physical_requests_attempted": backend.calls,
        "error_type": error_type,
        "offline_disclaimer": None
        if live
        else "Offline wiring only, not scientific quality evidence.",
        "evaluation_identity": expected,
        "metrics": snapshot.metrics,
        "provenance": provenance,
    }
    from evaluations.claim_support_eval import score_run

    claim_report = {
        "mode": "live" if live else "offline",
        "goal": GOALS[goal_id],
        "tier": tier,
        "completed": snapshot.metrics["completed"],
        "llm_calls": backend.calls,
        "latency_seconds": snapshot.metrics["wall_seconds"],
        "used_real_backend": live,
        "offline_disclaimer": receipt["offline_disclaimer"],
        **score_run(run_id, str(db)),
    }
    (output / f"claim-support-{'live' if live else 'offline'}.json").write_text(
        json.dumps({**claim_report, "provenance": provenance}, indent=2, allow_nan=False) + "\n"
    )
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, allow_nan=False) + "\n")
    m = snapshot.metrics
    header = (
        "| Goal | Complete | Featured | Screened | Verdict mix | Supported "
        "| Wall s | Calls | Tokens |\n"
    )
    divider = "|---|---|---|---|---|---|---|---|---|\n"
    row = [
        goal_id,
        m["completed"],
        m["ideas_featured"],
        m["ideas_screened"],
        json.dumps(m["verification_verdict_mix"], sort_keys=True),
        m["supported_claims"],
        m["wall_seconds"],
        m["physical_calls"],
        m["total_tokens"],
    ]
    table = (
        header
        + divider
        + "| "
        + " | ".join(str(v) if v is not None else "unknown" for v in row)
        + " |\n"
    )
    (output / "baseline.md").write_text(table)
    print(table, end="")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded fixed-goal benchmark collection.")
    parser.add_argument("--goal-id", choices=tuple(GOALS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-calls", type=int, default=150)
    parser.add_argument("--tier", choices=("express", "standard"), default="express")
    args = parser.parse_args()
    receipt = collect(
        args.goal_id,
        args.output,
        live=args.live,
        ceiling=args.max_calls,
        tier=args.tier,
    )
    return 0 if receipt["metrics"]["completed"] and receipt["error_type"] is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
