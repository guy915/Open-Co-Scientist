from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from typing import Any

from co_scientist.platform.db import connect, current_time, use_conn

logger = logging.getLogger(__name__)

ACTIVITY_PLANNING = "planning"
ACTIVITY_LITERATURE_SEARCH = "literature_search"
ACTIVITY_DRAFTING = "drafting"
ACTIVITY_REVIEW = "review"
ACTIVITY_TOURNAMENT = "tournament"
ACTIVITY_EVOLUTION = "evolution"
ACTIVITY_DEDUPLICATION = "deduplication"
ACTIVITY_SAFETY = "safety"
ACTIVITY_SYNTHESIS = "synthesis"
ACTIVITY_OTHER = "other"

ACTIVITY_VALUES: frozenset[str] = frozenset(
    {
        ACTIVITY_PLANNING,
        ACTIVITY_LITERATURE_SEARCH,
        ACTIVITY_DRAFTING,
        ACTIVITY_REVIEW,
        ACTIVITY_TOURNAMENT,
        ACTIVITY_EVOLUTION,
        ACTIVITY_DEDUPLICATION,
        ACTIVITY_SAFETY,
        ACTIVITY_SYNTHESIS,
        ACTIVITY_OTHER,
    }
)

_AGENT_ACTIVITY: dict[str, str] = {
    "supervisor": ACTIVITY_PLANNING,
    "reflection": ACTIVITY_REVIEW,
    "ranking": ACTIVITY_TOURNAMENT,
    "evolution": ACTIVITY_EVOLUTION,
    "proximity": ACTIVITY_DEDUPLICATION,
    "meta_review": ACTIVITY_SYNTHESIS,
    "safety": ACTIVITY_SAFETY,
}

# Generation owns multiple activities, so agent identity alone cannot resolve
# its event boundary.
_NODE_ACTIVITY_OVERRIDES: dict[str, str] = {
    "literature_review": ACTIVITY_LITERATURE_SEARCH,
    "generate": ACTIVITY_DRAFTING,
}

_TYPE_TO_NODE_ALIASES: dict[str, str] = {"supervisor.plan": "supervisor"}


def _node_to_agent() -> dict[str, str]:
    """Import lazily to keep store initialization independent of the engine."""
    from co_scientist.orchestration.registry import NODE_TO_AGENT

    return NODE_TO_AGENT


def _activity_for_node(node_name: str) -> str:
    override = _NODE_ACTIVITY_OVERRIDES.get(node_name)
    if override is not None:
        return override
    agent = _node_to_agent().get(node_name)
    if agent is None:
        return ACTIVITY_OTHER
    return _AGENT_ACTIVITY.get(agent, ACTIVITY_OTHER)


def activity_for_event(type_: str, payload: dict[str, Any]) -> str:
    if type_ == "scientific_task":
        task = payload.get("task")
        return _activity_for_node(str(task)) if task else ACTIVITY_OTHER
    if type_.startswith("safety"):
        return ACTIVITY_SAFETY
    node_name = _TYPE_TO_NODE_ALIASES.get(type_, type_)
    return _activity_for_node(node_name)


_stage_logger = logging.getLogger("app.run_stage")

_STAGE_MAX_CHARS = 200
_STAGE_VALUE_MAX_CHARS = 60


def _summarize_string_value(key: str, value: str) -> str | None:
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > _STAGE_VALUE_MAX_CHARS:
        text = text[:_STAGE_VALUE_MAX_CHARS] + "..."
    return f"{key}={text}"


def _summarize_value(key: str, value: Any) -> str | None:
    if isinstance(value, (bool, int, float)):
        return f"{key}={value}"
    if isinstance(value, str):
        return _summarize_string_value(key, value)
    if isinstance(value, (list, dict)):
        # Summaries retain collection sizes rather than copying private bulk
        # payload text into logs.
        return f"{key}={len(value)}"
    return None


def summarize_stage_payload(payload: dict[str, Any]) -> str:
    """Drop complete key/value pairs rather than truncating into a
    misleading key without its value.
    """
    parts: list[str] = []
    used = 0
    for key, value in payload.items():
        rendered = _summarize_value(key, value)
        if rendered is None:
            continue
        cost = len(rendered) + (1 if parts else 0)
        if used + cost > _STAGE_MAX_CHARS:
            parts.append("...")
            break
        parts.append(rendered)
        used += cost
    return " ".join(parts)


def _log_stage(run_id: str, type_: str, payload: dict[str, Any]) -> None:
    """Lazy import avoids the logging/store cycle; bind run context because
    RunIdFilter would otherwise replace the explicit run ID.
    """
    from co_scientist.platform.telemetry.logging_setup import run_log_context

    summary = summarize_stage_payload(payload)
    message = f"{type_} {summary}".strip()
    # RunIdFilter replaces explicit run_id from context; bind context before
    # emitting the mirrored record.
    with run_log_context(run_id):
        _stage_logger.info("%s", message, extra={"run_id": run_id, "diagnostic_event": type_})


def _append_event(
    conn: sqlite3.Connection,
    run_id: str,
    type_: str,
    payload: dict[str, Any],
    created_at: float,
    mirror_log: bool = True,
) -> int:
    """Allocate above both event and checkpoint high-water marks so cleared
    replay history never reuses a cursor already seen over SSE.
    """
    # Carry activity in payload to preserve the existing SSE schema.
    payload = {**payload, "activity": activity_for_event(type_, payload)}
    row = conn.execute(
        "INSERT INTO run_events (run_id, seq, type, payload_json, created_at) "
        "VALUES (?, 1 + MAX("
        "(SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id=?), "
        "(SELECT COALESCE(MAX(last_event_seq), 0) FROM checkpoints "
        "WHERE run_id=?)), ?, ?, ?) RETURNING seq",
        (run_id, run_id, run_id, type_, json.dumps(payload), created_at),
    ).fetchone()
    if mirror_log:
        log_event_stage(run_id, type_, payload)
    return int(row["seq"])


def log_event_stage(run_id: str, type_: str, payload: dict[str, Any]) -> None:
    """Mirrored logging is best-effort and must never fail the durable event
    write.
    """
    effective_payload = {
        **payload,
        "activity": activity_for_event(type_, payload),
    }
    with contextlib.suppress(Exception):
        _log_stage(run_id, type_, effective_payload)


def append_event(
    run_id: str,
    type_: str,
    payload: dict[str, Any],
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    with use_conn(conn, db_path) as active:
        return _append_event(active, run_id, type_, payload, current_time())


def append_event_deferred_log(
    run_id: str,
    type_: str,
    payload: dict[str, Any],
    conn: sqlite3.Connection,
) -> int:
    """Keep external logging after the caller's commit so rolled-back events
    never appear in the application log.
    """
    return _append_event(conn, run_id, type_, payload, current_time(), mirror_log=False)


def latest_event_seq(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    with use_conn(conn, db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return int(row[0])


def latest_status_event(
    run_id: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    with use_conn(conn, None) as active:
        row = active.execute(
            "SELECT payload_json FROM run_events "
            "WHERE run_id=? AND type='status' ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    return json.loads(row["payload_json"]) if row is not None else None


def list_events(
    run_id: str,
    after_seq: int = 0,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, type, payload_json, created_at FROM run_events "
            "WHERE run_id=? AND seq > ? ORDER BY seq ASC",
            (run_id, after_seq),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "seq": r["seq"],
                    "type": r["type"],
                    "payload": json.loads(r["payload_json"]),
                    "created_at": r["created_at"],
                }
            )
        return out
