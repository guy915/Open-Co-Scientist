import re
from collections.abc import Collection

_BRACKET_GROUP = re.compile(r"\[([^\[\]]+)\]")
_CITATION_KEY = re.compile(r"\AC\d+\Z")


def citation_keys_in(text: str, *, allowed_keys: Collection[str] | None = None) -> list[str]:
    # Generation uses C keys; attached sources retain their existing namespace.
    return [
        key
        for group in _BRACKET_GROUP.findall(text)
        for part in group.split(",")
        if (key := part.strip())
        and (key in allowed_keys if allowed_keys is not None else _CITATION_KEY.fullmatch(key))
    ]


# Author-year patterns come from paper-qa strip_citations (Apache-2.0). Numeric-
# only brackets preserve this engine's [C<n>] citation keys.
_CITATION_MARKER_RE = re.compile(
    r"\b[\w\-]+\set\sal\.\s\([0-9]{4}\)"
    r"|\((?:[^)]*?[a-zA-Z][^)]*?[0-9]{4}[^)]*?)\)"
    r"|\[[0-9]+(?:\s*[,\u2013-]\s*[0-9]+)*\]",
    re.MULTILINE,
)


def strip_citation_markers(text: str) -> str:
    """Strip only prompt-bound source copies; stored originals must retain
    published citations.
    """
    return _CITATION_MARKER_RE.sub("", text)
