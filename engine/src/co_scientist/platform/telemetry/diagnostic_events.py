"""Operational metadata never contains chat text or tool arguments."""

from __future__ import annotations

import logging
from contextlib import nullcontext

from co_scientist.platform.telemetry.logging_setup import run_log_context

logger = logging.getLogger("app.chat_turn")


def log_chat_turn(
    role: str,
    content: str,
    *,
    owner: str | None = None,
    run_id: str | None = None,
    duration_seconds: float = 0,
) -> None:
    with run_log_context(run_id) if run_id else nullcontext():
        logger.info(
            "chat_turn role=%s chars=%d duration_seconds=%.3f",
            role,
            len(content),
            max(0, duration_seconds),
            extra={"client_id": owner},
        )
