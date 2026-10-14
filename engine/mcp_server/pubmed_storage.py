import json
import re
from pathlib import Path
from typing import Any

_CACHE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PUBMED_ID = re.compile(r"^[0-9]{1,16}$")


def validate_cache_identifier(value: str, *, label: str, numeric: bool = False) -> str:
    pattern = _PUBMED_ID if numeric else _CACHE_COMPONENT
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def confined_path(root: Path, *parts: str) -> Path:
    base = root.resolve()
    candidate = base.joinpath(*parts)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("literature cache path escapes its root") from exc
    return candidate


def write_metadata_cache_file(metadata_file: Path, metadata: dict[str, Any]) -> None:
    if metadata_file.is_symlink():
        raise ValueError("metadata cache file must not be a symlink")
    with open(metadata_file, "w", encoding="utf-8") as stream:
        json.dump(metadata, stream)
