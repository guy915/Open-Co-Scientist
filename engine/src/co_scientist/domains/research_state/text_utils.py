from __future__ import annotations

import re
from typing import Any

from co_scientist.core.text_matching import coverage, tokenize

# Require whitespace or end after sentence punctuation so abbreviations such as
# M.tuberculosis are not split into truncated titles.
_SENTENCE_END = re.compile(r"[.?!](\s|$)")


def coalesce(*values: Any, default: Any = "") -> Any:
    for value in values:
        if value:
            return value
    return default


def hypothesis_title(h: dict[str, Any]) -> str:
    return str(h.get("title") or h.get("text") or "Untitled")[:140]


def hypothesis_statement(h: dict[str, Any]) -> str:
    """Never substitute a truncated title for missing proposal text; store
    statement and engine text must resolve consistently across report
    sections.
    """
    return str(h.get("statement") or h.get("text") or "")


def hypothesis_id(h: dict[str, Any]) -> str:
    return str(h.get("id") or h.get("hypothesis_id") or hypothesis_title(h))


def first_sentence(text: str) -> str:
    """Store the full first sentence; display word-truncation must not
    become a permanently persisted mid-word title stub.
    """
    match = _SENTENCE_END.search(text)
    return (text[: match.start()] if match else text).strip()


_ORDINAL_PREFIX = re.compile(r"^\d+\.\s*")


def readable_experiment_summary(text: str) -> str:
    """These consumers render plain text or limited HTML, not Markdown;
    collapse pilot-plan markup without losing its prose.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    parts = []
    for line in lines:
        line = _ORDINAL_PREFIX.sub("", line)
        line = line.replace("**Go:**", "Go:").replace("**No-Go:**", "No-Go:")
        parts.append(line)
    return " ".join(parts)


def token_coverage(text: str, reference: str) -> float:
    """Union denominators hide contained short ideas; divide by the derived
    text tokens so true coverage remains detectable across lengths."""
    return coverage(frozenset(tokenize(text)), frozenset(tokenize(reference)))
