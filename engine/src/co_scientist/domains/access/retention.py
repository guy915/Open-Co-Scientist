from __future__ import annotations

import logging
import os
import time

from co_scientist.domains.documents import repository as store
from co_scientist.platform.db import runs as store_runs
from co_scientist.platform.db.admission_retention import purge_expired_admission_history

logger = logging.getLogger(__name__)

_DEFAULT_RUN_RETENTION_DAYS = 90
_DEFAULT_DRAFT_RETENTION_DAYS = 7
_DEFAULT_DOCUMENT_RETENTION_DAYS = 30
_SECONDS_PER_DAY = 86_400


def _retention_days(env_var: str, default_days: int) -> int:
    """Malformed maintenance settings must not become fatal startup or cron
    work.
    """
    raw = os.getenv(env_var)
    if not raw:
        return default_days
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Ignoring invalid %s", env_var)
        return default_days


def document_retention_days() -> int:
    return _retention_days(
        "COSCIENTIST_DOCUMENT_RETENTION_DAYS",
        _DEFAULT_DOCUMENT_RETENTION_DAYS,
    )


def sweep_expired_runs(*, now: float | None = None) -> list[str]:
    """Use short per-run transactions rather than holding SQLite's writer
    across the whole retention sweep.
    """
    now = time.time() if now is None else now
    deleted = []
    for env_var, default, lookup in (
        (
            "COSCIENTIST_RUN_RETENTION_DAYS",
            _DEFAULT_RUN_RETENTION_DAYS,
            store_runs.list_expired_terminal_runs,
        ),
        (
            "COSCIENTIST_DRAFT_RETENTION_DAYS",
            _DEFAULT_DRAFT_RETENTION_DAYS,
            store_runs.list_expired_draft_runs,
        ),
    ):
        days = _retention_days(env_var, default)
        if days == 0:
            continue
        for run in lookup(now - days * _SECONDS_PER_DAY):
            cutoff = now - days * _SECONDS_PER_DAY
            counts = store_runs.delete_run(
                run.id,
                draft_before=cutoff if lookup is store_runs.list_expired_draft_runs else None,
            )
            if not counts:
                continue
            deleted.append(run.id)
            logger.info("Retention swept expired run %s", run.id)
    return deleted


def sweep_expired_documents(*, now: float | None = None) -> int:
    days = document_retention_days()
    if days == 0:
        return 0
    cutoff = (time.time() if now is None else now) - days * _SECONDS_PER_DAY
    count = store.delete_staged_documents_older_than(cutoff)
    if count:
        logger.info("Retention swept %d expired staged document(s)", count)
    return count


def sweep_all(*, now: float | None = None) -> dict[str, int]:
    runs = sweep_expired_runs(now=now)
    documents = sweep_expired_documents(now=now)
    return {
        "runs": len(runs),
        "documents": documents,
        "admission_history": purge_expired_admission_history(now=now),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = sweep_all()
    logger.info(
        "Retention sweep complete: runs=%d documents=%d",
        results["runs"],
        results["documents"],
    )
