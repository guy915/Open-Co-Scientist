from __future__ import annotations

import re

_MINOR_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "from",
        "in",
        "into",
        "nor",
        "of",
        "on",
        "or",
        "per",
        "the",
        "to",
        "versus",
        "via",
        "vs",
        "with",
    }
)
_LOWER_WORD = re.compile("[a-z][a-z'\u2019-]*")


def _title_word(word: str, edge: bool) -> str:
    start = 0
    while start < len(word) and not word[start].isalnum():
        start += 1
    end = len(word)
    while end > start and not word[end - 1].isalnum():
        end -= 1
    core = word[start:end]
    if not _LOWER_WORD.fullmatch(core) or (not edge and core in _MINOR_WORDS):
        return word
    return word[:start] + core[0].upper() + core[1:] + word[end:]


def title_case(text: str) -> str:
    """Only all-lowercase ASCII words change, so mRNA, FDA-approved, p53 and
    β-catenin keep their authors' casing; the frontend's titleCase matches.
    """
    parts = re.split(r"(\s+)", text)
    words = [i for i, part in enumerate(parts) if part and not part.isspace()]
    edges = {words[0], words[-1]} if words else set()
    return "".join(
        part if i not in words else _title_word(part, i in edges) for i, part in enumerate(parts)
    )
