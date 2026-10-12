import configparser
from pathlib import Path

# Lower this with every PR that deletes ignore entries; it never rises.
IGNORED_IMPORTS_CEILING = 0

_CONFIG = Path(__file__).resolve().parents[2] / ".importlinter"


def _ignored_imports() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(_CONFIG)
    return [
        line.strip()
        for section in parser.sections()
        for line in parser[section].get("ignore_imports", "").splitlines()
        if line.strip()
    ]


def test_ignored_imports_only_shrink() -> None:
    assert len(_ignored_imports()) == IGNORED_IMPORTS_CEILING


def test_ignored_imports_name_single_imports() -> None:
    entries = _ignored_imports()
    assert all("*" not in entry and entry.count(" -> ") == 1 for entry in entries)
    assert len(entries) == len(set(entries))
