"""Bounded owner-scoped feedback with durable admission budgets."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from co_scientist.core.admission_windows import WindowExceededError
from co_scientist.platform import db
from co_scientist.platform.db.admission_windows import reserve_feedback_window

RateExceededError = WindowExceededError

Category = Literal["Bug", "Security", "Results quality", "Feature request", "Other"]
MAX_ROWS = 200
MAX_BYTES = 10 * 1024 * 1024
RETENTION_SECONDS = 30 * 24 * 60 * 60
OWNER_PER_MINUTE = 5
HOST_PER_MINUTE = 20
GLOBAL_PER_MINUTE = 100


@dataclass(frozen=True)
class Submission:
    category: Category
    message: str
    diagnostics: str
    url: str
    run_id: str | None = None


def submit(owner: str, host_key: str, submission: Submission) -> str:
    now = db.current_time()
    identity = str(uuid.uuid4())
    size = (
        sum(
            len(text.encode("utf-8"))
            for text in (
                identity,
                owner,
                submission.category,
                submission.message,
                submission.diagnostics,
                submission.url,
                submission.run_id or "",
            )
        )
        + 128
    )
    if size > MAX_BYTES:
        raise ValueError("feedback exceeds the storage limit")
    with db.transaction() as conn:
        reserve_feedback_window(
            conn,
            owner,
            host_key,
            now=now,
            owner_limit=OWNER_PER_MINUTE,
            host_limit=HOST_PER_MINUTE,
            global_limit=GLOBAL_PER_MINUTE,
            detail="Please wait a minute before sending more feedback.",
        )
        conn.execute(
            "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?)",
            (
                identity,
                owner,
                submission.category,
                submission.message,
                submission.diagnostics,
                submission.url,
                submission.run_id,
                now,
                size,
            ),
        )
        conn.execute(
            "DELETE FROM feedback WHERE created_at<?",
            (now - RETENTION_SECONDS,),
        )
        # Keep newest records within both budgets, including multibyte text.
        conn.execute(
            "DELETE FROM feedback WHERE id IN (SELECT id FROM ("
            "SELECT id,ROW_NUMBER() OVER "
            "(ORDER BY created_at DESC,rowid DESC) AS n,"
            "SUM(byte_size) OVER "
            "(ORDER BY created_at DESC,rowid DESC) AS bytes "
            "FROM feedback) WHERE n>? OR bytes>?)",
            (MAX_ROWS, MAX_BYTES),
        )
    return identity
