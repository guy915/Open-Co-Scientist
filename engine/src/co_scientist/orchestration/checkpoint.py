"""Runtime handles are re-injected on restore; incompatible checkpoint
versions fail closed.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    messages_from_dict,
    messages_to_dict,
)

from co_scientist.domains.research_state.models import ExecutionMetrics, Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.db.checkpoints import CHECKPOINT_VERSION
from co_scientist.platform.retrieval.article import Article

# Runtime collection objects need explicit JSON serialization.
_SPECIAL_COLLECTION_KEYS = frozenset({"hypotheses", "metrics", "articles", "messages"})


def _serialize_messages(messages: Any) -> list[dict[str, Any]]:
    """LangGraph add_messages produces BaseMessage objects, which are not
    JSON-serializable.
    """
    items = list(messages or [])
    if items and all(isinstance(m, BaseMessage) for m in items):
        return messages_to_dict(items)
    return items


def _deserialize_messages(raw: Any) -> list[Any]:
    """add_messages accepts message objects and dicts; already-plain shapes
    pass through.
    """
    items = list(raw or [])
    if items and all(isinstance(m, dict) and "type" in m and "data" in m for m in items):
        return list(messages_from_dict(items))
    return items


# Runtime handles never serialized; re-injected on restore from the live run.
_EXCLUDED_RUNTIME_KEYS = frozenset({"progress_callback", "tool_registry"})

# Do not checkpoint attempt-local retry rights or steering flags; restore them
# from durable owners.
_TRANSIENT_CONTROL_KEYS = frozenset({"resume", "pending_steering", "durable_retries_remain"})

# Persist active seconds so checkpoint pauses do not consume wall-clock budgets.
_REBASED_TIME_KEYS = frozenset({"start_time"})

# Derive fields in declaration order: new state channels persist unless
# explicitly excluded.
_PLAIN_STATE_KEYS: tuple[str, ...] = tuple(
    key
    for key in WorkflowState.__annotations__
    if key
    not in (
        _SPECIAL_COLLECTION_KEYS
        | _EXCLUDED_RUNTIME_KEYS
        | _TRANSIENT_CONTROL_KEYS
        | _REBASED_TIME_KEYS
    )
)


class CheckpointSchemaError(Exception):
    """Incompatible checkpoint versions fail closed to prevent resumed-state
    corruption.
    """


def _serialize_typed_collections(state: dict[str, Any]) -> dict[str, Any]:
    metrics = state.get("metrics")
    articles = state.get("articles")
    return {
        "hypotheses": [h.to_dict() for h in state.get("hypotheses", [])],
        "metrics": (
            metrics.to_dict()
            if isinstance(metrics, ExecutionMetrics)
            else ExecutionMetrics().to_dict()
        ),
        "articles": ([a.to_dict() for a in articles] if articles else articles),
        "messages": _serialize_messages(state.get("messages")),
    }


def serialize_workflow_state(
    state: dict[str, Any],
    *,
    last_event_seq: int,
    prompt_version: str = "",
    config_version: str = "",
) -> dict[str, Any]:
    """Checkpoint only committed state; resumed event sequences must exceed
    the durable high-water mark.
    """
    payload: dict[str, Any] = {key: state.get(key) for key in _PLAIN_STATE_KEYS}
    start_time = state.get("start_time")
    payload["elapsed_active_s"] = max(0.0, time.time() - float(start_time)) if start_time else 0.0
    payload.update(_serialize_typed_collections(state))

    return {
        "version": CHECKPOINT_VERSION,
        "last_event_seq": last_event_seq,
        "prompt_version": prompt_version,
        "config_version": config_version,
        "state": payload,
    }


def _restore_typed_collections(payload: dict[str, Any]) -> None:
    payload["hypotheses"] = [Hypothesis.from_dict(h) for h in payload.get("hypotheses", [])]
    payload["metrics"] = ExecutionMetrics.from_dict(payload.get("metrics") or {})
    articles = payload.get("articles")
    payload["articles"] = [Article.from_dict(a) for a in articles] if articles else articles
    payload["messages"] = _deserialize_messages(payload.get("messages"))


def _rebase_start_time(payload: dict[str, Any]) -> None:
    """Exclude paused time; older version-1 checkpoints without
    elapsed_active_s retain their timestamp.
    """
    elapsed_active_s = payload.pop("elapsed_active_s", None)
    if elapsed_active_s is not None:
        payload["start_time"] = time.time() - float(elapsed_active_s)


def restore_workflow_state(
    checkpoint: dict[str, Any],
    *,
    progress_callback: Any = None,
    tool_registry: Any = None,
) -> dict[str, Any]:
    version = checkpoint.get("version")
    if version != CHECKPOINT_VERSION:
        raise CheckpointSchemaError(
            f"checkpoint version {version!r} is incompatible with "
            f"CHECKPOINT_VERSION {CHECKPOINT_VERSION}"
        )

    payload = dict(checkpoint.get("state", {}))
    _restore_typed_collections(payload)
    _rebase_start_time(payload)
    for key in _EXCLUDED_RUNTIME_KEYS:
        payload.pop(key, None)
    payload["progress_callback"] = progress_callback
    payload["tool_registry"] = tool_registry
    payload["resume"] = True
    return payload


def last_event_seq(checkpoint: dict[str, Any]) -> int:
    return int(checkpoint.get("last_event_seq", 0))
