"""Hidden interrupted calls invite repeated effects; retain aborted results.
Providers require positional call/result pairing and matching IDs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from litellm.litellm_core_utils.streaming_chunk_builder_utils import ChunkProcessor

from co_scientist.platform.sandbox.workspace.tool_schemas import WRITE_FILE

logger = logging.getLogger(__name__)

# Interrupted commands may already have side effects; never claim they did not
# run.
ABORTED_RESULT = {
    "error": "aborted",
    "detail": (
        "This call was interrupted before its result was recorded. It "
        "may have run and had effects. Check the current state before "
        "repeating it."
    ),
}


# A truncated call is answered with this instead of being executed.
INVALID_ARGUMENTS_ERROR = (
    "The arguments of this call were not a complete JSON object (likely"
    " truncated), so it was not run. Re-issue the call with complete"
    " arguments."
)


def object_arguments(raw: Any) -> str | None:
    if raw is None or raw == "":
        return "{}"
    if isinstance(raw, dict):
        return json.dumps(raw)
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return raw if isinstance(parsed, dict) else None


class ThinkingTranscript:
    def __init__(self) -> None:
        self._chunks: list[dict[str, Any]] = []

    def observe(self, delta: Any) -> None:
        blocks = getattr(delta, "thinking_blocks", None)
        if blocks:
            self._chunks.append({"choices": [{"delta": {"thinking_blocks": blocks}}]})

    def blocks(self) -> list[dict[str, Any]]:
        if not self._chunks:
            return []
        # The pinned SDK joins signed/redacted fragments without rebuilding
        # signatures or retaining unrelated streamed content.
        processor = ChunkProcessor(self._chunks)
        return [
            dict(block) for block in processor.get_combined_thinking_content(self._chunks) or []
        ]


def _message_to_history_dict(message: Any) -> dict[str, Any]:
    """LiteLLM messages are Pydantic models; history entries need plain
    dictionaries.
    """
    message_dict: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }

    # DeepSeek requires reasoning_content echoed on assistant tool calls or
    # rejects the next turn.
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        message_dict["reasoning_content"] = reasoning

    # Adaptive-thinking tool turns require the original signed blocks.
    thinking_blocks = getattr(message, "thinking_blocks", None)
    if thinking_blocks:
        message_dict["thinking_blocks"] = thinking_blocks

    items = getattr(message, "responses_items", None)
    if items:
        message_dict["responses_items"] = items

    if hasattr(message, "tool_calls") and message.tool_calls:
        message_dict["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                # Providers reject every later turn that echoes malformed
                # arguments.
                "function": {
                    "name": tc.function.name,
                    "arguments": object_arguments(tc.function.arguments) or "{}",
                },
            }
            for tc in message.tool_calls
        ]

    return message_dict


def _answered_ids(messages: list[dict[str, Any]]) -> set[str]:
    return {
        str(message.get("tool_call_id"))
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id")
    }


def _requested_ids(messages: list[dict[str, Any]]) -> set[str]:
    ids = set()
    for message in messages:
        for call in message.get("tool_calls") or ():
            if isinstance(call, dict) and call.get("id"):
                ids.add(str(call["id"]))
    return ids


def _aborted_for(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") or {}
    return {
        "role": "tool",
        "tool_call_id": str(call.get("id")),
        "name": str(function.get("name") or "unknown"),
        "content": json.dumps(ABORTED_RESULT),
    }


def _with_results(message: dict[str, Any], answered: set[str]) -> list[dict[str, Any]]:
    missing = [
        call
        for call in message.get("tool_calls") or ()
        if isinstance(call, dict) and str(call.get("id")) not in answered
    ]
    if not missing:
        return [message]
    logger.info(
        "Synthesizing %s aborted tool result(s) for an interrupted turn",
        len(missing),
    )
    return [message, *(_aborted_for(call) for call in missing)]


def normalize_tool_transcript(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Repair without mutating cached inputs shared with another caller."""
    answered = _answered_ids(messages)
    requested = _requested_ids(messages)
    repaired: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool":
            if str(message.get("tool_call_id")) in requested:
                repaired.append(message)
            else:
                logger.info("Dropping a tool result answering no call")
            continue
        repaired.extend(_with_results(message, answered))
    return repaired


# Keep the path and explain elision so missing text is not mistaken for a failed
# write.
_SUPERSEDED_NOTE = (
    "superseded by a later write to this path; the file on disk holds the"
    " current text, read it if you need it"
)


def _written_path(call: dict[str, Any]) -> str | None:
    """Patches are relative edits; a later patch never supersedes an earlier
    one.
    """
    function = call.get("function") or {}
    if function.get("name") != WRITE_FILE:
        return None
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (TypeError, ValueError):
        return None
    path = arguments.get("path")
    return path if isinstance(path, str) else None


def elide_superseded_writes(messages: list[dict[str, Any]]) -> int:
    """Preserve call IDs and results; superseded whole-file text remains
    readable from disk.
    """
    latest: dict[str, dict[str, Any]] = {}
    elided = 0
    for message in messages:
        for call in message.get("tool_calls") or ():
            path = _written_path(call)
            if path is None:
                continue
            previous = latest.get(path)
            if previous is not None:
                previous["arguments"] = json.dumps({"path": path, "note": _SUPERSEDED_NOTE})
                elided += 1
            latest[path] = call["function"]
    return elided


# Every configured search source stamps at least one of these identity fields.
_PAPER_ID_FIELDS = ("source_id", "pmid", "doi", "nct_id", "url")

# Leave already-small entity lookup payloads alone; their whole payload is the
# answer.
_PAPER_BODY_FIELDS = ("abstract", "abstractText", "content", "fulltext")

# Preserve enough to cite and assess a paper; author lists are unnecessary for
# ID-based citations.
_ELIDED_MARKER = "elided"
_KEPT_FIELDS = (
    *_PAPER_ID_FIELDS,
    "title",
    "year",
    "journal",
    "is_preprint",
    _ELIDED_MARKER,
)

# Point to refetch by ID; the original copy may already have aged out of the
# transcript.
_ELIDED_NOTE = "full text elided; re-fetch by id if you need it"

# For unstructured documents the retained call identifies how to fetch the text
# again.
_REFETCHABLE_TEXT_TOOLS = ("read_url",)

_ELIDED_TEXT = json.dumps(
    "Elided: you read this in full earlier in this conversation. Call the"
    " same tool again with the same arguments if you need it back."
)

# Whole evidence must arrive and remain long enough for gap judgment before
# ageing.
RECENT_SEARCH_TURNS = 2


def _paper_identity(record: dict[str, Any]) -> str | None:
    for field in _PAPER_ID_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value:
            return f"{field}:{value}"
    return None


def _has_body(record: dict[str, Any]) -> bool:
    """Already-elided stubs must not erase the only live copy during
    deduplication.
    """
    return any(record.get(field) for field in _PAPER_BODY_FIELDS)


def _elide_record(record: dict[str, Any]) -> int:
    for key in [key for key in record if key not in _KEPT_FIELDS]:
        del record[key]
    record[_ELIDED_MARKER] = _ELIDED_NOTE
    return 1


def _walk_papers(node: Any, visit: Callable[[dict[str, Any]], int]) -> int:
    """Recognize records rather than envelopes so new source shapes are
    covered automatically.
    """
    if isinstance(node, list):
        return sum(_walk_papers(item, visit) for item in node)
    if not isinstance(node, dict):
        return 0
    if _paper_identity(node) is not None and _has_body(node):
        return visit(node)
    return sum(_walk_papers(value, visit) for value in node.values())


def _rewrite_tool_result(message: dict[str, Any], transform: Callable[[Any], int]) -> int:
    if message.get("role") != "tool":
        return 0
    content = message.get("content")
    if not isinstance(content, str) or not content.startswith(("{", "[")):
        return 0
    try:
        payload = json.loads(content)
    except ValueError:
        return 0
    changed = transform(payload)
    if changed:
        message["content"] = json.dumps(payload)
    return changed


def elide_repeated_papers(messages: list[dict[str, Any]]) -> int:
    """Run after ageing: an old stub is no longer a copy against which fresh
    text can be elided.
    """
    seen: set[str] = set()

    def visit(record: dict[str, Any]) -> int:
        identity = str(_paper_identity(record))
        if identity in seen:
            return _elide_record(record)
        seen.add(identity)
        return 0

    return sum(
        _rewrite_tool_result(message, lambda payload: _walk_papers(payload, visit))
        for message in messages
    )


def _aged_before(messages: list[dict[str, Any]], turns: int) -> int:
    assistants = [
        index for index, message in enumerate(messages) if message.get("role") == "assistant"
    ]
    if len(assistants) < turns:
        return 0
    return assistants[-turns]


def _elide_text_result(message: dict[str, Any]) -> int:
    """Unstructured documents have no citation fields; the retained call
    identifies how to refetch.
    """
    if message.get("name") not in _REFETCHABLE_TEXT_TOOLS:
        return 0
    content = message.get("content")
    if not isinstance(content, str) or content == _ELIDED_TEXT:
        return 0
    message["content"] = _ELIDED_TEXT
    return 1


def _elide_aged_message(message: dict[str, Any]) -> int:
    if message.get("role") != "tool":
        return 0
    return _rewrite_tool_result(
        message, lambda payload: _walk_papers(payload, _elide_record)
    ) + _elide_text_result(message)


def elide_aged_evidence(messages: list[dict[str, Any]], turns: int = RECENT_SEARCH_TURNS) -> int:
    """Deduplication cannot bound distinct papers; age only after the model
    had whole evidence to use.
    """
    cutoff = _aged_before(messages, turns)
    return sum(_elide_aged_message(message) for message in messages[:cutoff])
