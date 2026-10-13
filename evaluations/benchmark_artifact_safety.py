from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import quote, quote_plus

_CHUNK_BYTES = 1024 * 1024
_CREDENTIAL_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENALEX_API_KEY",
    "TAVILY_API_KEY",
    "COSCIENTIST_MCP_SHARED_SECRET",
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
)


def _patterns(credentials: Iterable[str]) -> tuple[bytes, ...]:
    forms: set[str] = set()
    for credential in credentials:
        if credential:
            for value in (credential, credential.strip()):
                if value:
                    forms.update(
                        (value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1])
                    )
    return tuple(value.encode() for value in forms)


def _check_file(path: Path, patterns: tuple[bytes, ...]) -> None:
    if not patterns:
        return
    overlap = max(map(len, patterns)) - 1
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            data = tail + chunk
            if any(pattern in data for pattern in patterns):
                raise ValueError("benchmark artifacts contain a configured credential")
            tail = data[-overlap:] if overlap else b""


def check_artifacts(paths: Sequence[Path], credentials: Iterable[str]) -> None:
    patterns = _patterns(credentials)
    for path in paths:
        if path.is_symlink():
            raise ValueError("benchmark artifacts must not contain symbolic links")
        if not path.exists():
            continue
        members = path.rglob("*") if path.is_dir() else (path,)
        for member in members:
            if member.is_symlink():
                raise ValueError("benchmark artifacts must not contain symbolic links")
            if member.is_file():
                _check_file(member, patterns)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check benchmark outputs before publication.")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    check_artifacts(args.paths, (os.getenv(name, "") for name in _CREDENTIAL_NAMES))
    print("Benchmark artifact credential check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
