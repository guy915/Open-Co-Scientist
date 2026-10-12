from __future__ import annotations

from co_scientist.platform.db import connect, current_time


def purge_expired_admission_history(*, now: float | None = None) -> int:
    now = current_time() if now is None else now
    cutoff = now - 7 * 86_400
    day = int(now // 86_400) - 6
    count = 0
    policies: list[tuple[str, str, int | float]] = [
        (table, "day<?", day)
        for table in (
            "anonymous_admissions",
            "provider_admissions",
            "input_admissions",
            "app_llm_usage",
            "provider_token_reservations",
        )
    ]
    policies.extend(
        (table, "created_at<?", cutoff) for table in ("feedback_admissions", "free_run_usage")
    )
    policies.extend(
        (
            ("log_ingest_admissions", "minute<?", int(cutoff // 60)),
            # A live run's host mapping is needed for future continuation and
            # concurrency admission, even when the run is months old.
            (
                "run_admissions",
                "day<? AND NOT EXISTS (SELECT 1 FROM runs WHERE id=run_admissions.run_id)",
                day,
            ),
        )
    )
    for table, condition, boundary in policies:
        with connect() as conn:
            deleted = conn.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {table} WHERE {condition} LIMIT 500)",
                (boundary,),
            )
            count += max(0, deleted.rowcount)
    return count
