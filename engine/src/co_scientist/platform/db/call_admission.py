from co_scientist.core.exceptions import LLMCallBudgetExceededError
from co_scientist.platform.db import connect, transaction


def ensure_run_counter(run_id: str, ceiling: int | None, *, db_path: str) -> bool:
    if ceiling is None:
        return read_run_count(run_id, db_path=db_path) is not None
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO run_call_admissions(run_id,calls,ceiling) VALUES (?,0,?)",
            (run_id, ceiling),
        )
    return True


def reserve_run_call(run_id: str, *, db_path: str) -> None:
    # Failed and interrupted dispatches retain their reservation; deletion or
    # terminal cleanup must not refund attempts with uncertain billing.
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT calls,ceiling FROM run_call_admissions WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise LLMCallBudgetExceededError(1, 0)
        count, ceiling = int(row["calls"]) + 1, int(row["ceiling"])
        if count > ceiling:
            raise LLMCallBudgetExceededError(count, ceiling)
        conn.execute("UPDATE run_call_admissions SET calls=? WHERE run_id=?", (count, run_id))


def read_run_count(run_id: str, *, db_path: str) -> int | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT calls FROM run_call_admissions WHERE run_id=?", (run_id,)
        ).fetchone()
    return int(row["calls"]) if row else None
