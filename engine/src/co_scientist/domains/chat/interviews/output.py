from __future__ import annotations

import json
import re
from typing import Any

# Only a line-leading JSON value can be the model's untagged control payload.
_CANDIDATE = re.compile(r"(?m)^[ \t]*(?=[{\[`])")
_SPEC_KEYS = frozenset({"research_challenge", "focus_area", "lab_constraints", "questions"})
_DECODER = json.JSONDecoder()


def _metadata(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and _SPEC_KEYS.intersection(value):
        return value
    if _question(value):
        return {"questions": [value]}
    if isinstance(value, list) and value and all(_question(question) for question in value):
        return {"questions": value}
    return None


def _question(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("question"), str)
        and isinstance(value.get("options"), list)
    )


def _json_prefix(text: str) -> bool:
    stripped = text.lstrip()
    while stripped.startswith("["):
        stripped = stripped[1:].lstrip()
    if not stripped:
        return True
    if stripped.startswith("{"):
        tail = stripped[1:].lstrip()
        return not tail or tail.startswith(('"', "}"))
    return False


class InterviewProse:
    """Models sometimes repeat control JSON outside its tag. Hold candidates
    until their shape is known so streaming never renders and retracts them.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._line_start = True
        self.fields: dict[str, Any] | None = None

    def feed(self, fragment: str) -> str:
        self._pending += fragment
        released: list[str] = []
        while self._pending:
            match = _CANDIDATE.search(self._pending)
            if match is not None and match.start() == 0 and not self._line_start:
                match = _CANDIDATE.search(self._pending, 1)
            if match is None:
                released.append(self._take(len(self._pending)))
                break
            if match.start():
                released.append(self._take(match.start()))
            candidate = self._pending.lstrip(" \t")
            if candidate.startswith("`"):
                end = self._fence_end(candidate)
            else:
                end = self._json_end(candidate)
            if end is None:
                break
            if end == 0:
                released.append(self._take(1))
                continue
            length = len(self._pending) - len(candidate) + end
            block = self._pending[:length]
            self._take(length)
            message = self._consume_metadata(candidate[:end])
            released.append(block if message is None else message)
        return "".join(released)

    def _take(self, length: int) -> str:
        text, self._pending = self._pending[:length], self._pending[length:]
        self._line_start = text.endswith("\n") or (self._line_start and not text.strip(" \t"))
        return text

    def _json_end(self, text: str) -> int | None:
        if not _json_prefix(text):
            return 0
        try:
            _, end = _DECODER.raw_decode(text)
            return end
        except ValueError:
            return None

    def _fence_end(self, text: str) -> int | None:
        if text in ("`", "``"):
            return None
        if not text.startswith("```"):
            return 0
        end = re.search(r"(?m)^```[ \t]*(?:\r?\n|$)", text[3:])
        return None if end is None else 3 + end.end()

    def _consume_metadata(self, text: str) -> str | None:
        body = text
        if text.startswith("```"):
            first, _, rest = text.partition("\n")
            if first.strip() not in ("```", "```json"):
                return None
            body = rest.rsplit("```", 1)[0].strip()
        try:
            value = json.loads(body)
        except ValueError:
            return None
        fields = _metadata(value)
        if fields is None:
            return None
        self.fields = {**(self.fields or {}), **fields}
        message = fields.get("assistant_message")
        return message if isinstance(message, str) else ""

    def finish(self, *, tagged: bool = False) -> str:
        text, self._pending = self._pending, ""
        # An opening JSON fence immediately before the canonical tag is just
        # another wrapper around hidden metadata, not a visible code example.
        if tagged and text.strip() in ("```", "```json"):
            return ""
        body = text.lstrip()
        if body.startswith(("```json\n", "```\n")):
            body = body.partition("\n")[2]
        if _json_prefix(body) and (
            any(re.search(rf'"{key}"\s*:', body) for key in _SPEC_KEYS)
            or (all(re.search(rf'"{key}"\s*:', body) for key in ("question", "options")))
        ):
            return ""
        return text
