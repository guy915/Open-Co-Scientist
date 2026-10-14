import re
from collections.abc import Collection, Set

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str, *, min_len: int = 1, stopwords: Collection[str] = ()) -> tuple[str, ...]:
    # Order and duplicates carry term frequency; scorers explicitly form sets.
    return tuple(
        token
        for token in _WORD.findall(text.casefold())
        if len(token) >= min_len and token not in stopwords
    )


def coverage(wanted: Set[str], found: Set[str]) -> float:
    return len(wanted & found) / len(wanted) if wanted else 0.0
