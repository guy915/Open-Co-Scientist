from __future__ import annotations

import sqlite3
from typing import Any


def email_notifications_configured() -> bool:
    # Keep completion mail unavailable until recipient consent and delivery
    # admission exist.
    return False


async def deliver_completion_notification(
    inputs: dict[str, Any],
) -> dict[str, str]:
    # Old durable tasks contain unverified recipients too; finish without
    # network access so restart/retry cannot revive anonymous mail delivery.
    return {"status": "disabled"}


def enqueue_completion_notification(
    run_id: str,
    research_goal: str,
    report_id: str,
    *,
    db_path: str | None,
    conn: sqlite3.Connection | None = None,
) -> None:
    # Preserve legacy callers without authorizing public mail.
    return
