from __future__ import annotations

from co_scientist.orchestration import task_worker


async def drain_run_tasks(
    run_id: str,
    worker_id: str,
    *,
    db_path: str | None = None,
    lease_seconds: float = 300.0,
) -> None:
    """Drain a run's durable tasks in tests using the production worker step."""
    while await task_worker.run_once(
        worker_id,
        run_id=run_id,
        db_path=db_path,
        lease_seconds=lease_seconds,
    ):
        pass
