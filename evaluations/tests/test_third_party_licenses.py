import json
import re
from importlib import metadata
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LOCKS = ("api", "mcp", "skills")
_REVIEW = json.loads((_ROOT / "requirements/licenses.json").read_text())

# AGPL reaches users over the network; the rest restrict use or redistribution.
_BLOCKED = re.compile(
    r"\bA?GPL|(?<!Lesser )General Public|\bSSPL|Server Side Public|\bBUSL"
    r"|Business Source|Elastic License|Commons Clause|NonCommercial|NoDerivatives"
    r"|\bCC-BY-N[CD]|\bproprietary\b",
    re.IGNORECASE,
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked() -> set[str]:
    names: set[str] = set()
    for lock in _LOCKS:
        text = (_ROOT / f"requirements/{lock}.txt").read_text()
        names.update(_canonical(m) for m in re.findall(r"^([A-Za-z0-9._-]+)==", text, re.M))
    return names


def _spdx_ids(expression: str) -> set[str]:
    tokens = re.split(r"[()\s]+", expression)
    return {token for token in tokens if token and token not in {"AND", "OR", "WITH"}}


def test_every_locked_distribution_has_a_reviewed_licence() -> None:
    reviewed = set(_REVIEW["distributions"])
    assert sorted(_locked() - reviewed) == []
    assert sorted(reviewed - _locked()) == []


def test_reviewed_licences_are_on_the_allowlist() -> None:
    allowed = set(_REVIEW["allowed"])
    rejected = {
        name: expression
        for name, expression in _REVIEW["distributions"].items()
        if not _spdx_ids(expression) or not _spdx_ids(expression) <= allowed
    }
    assert rejected == {}


def _declared(distribution: metadata.Distribution) -> str:
    fields = distribution.metadata.json
    license_line = str(fields.get("license", "")).strip().splitlines()[:1]
    classifiers = [c for c in fields.get("classifier", []) if c.startswith("License")]
    expression = str(fields.get("license_expression", ""))
    return " | ".join(part for part in [expression, *license_line, *classifiers] if part)


@pytest.mark.parametrize("blocked", ["AGPL-3.0-only", "GPL-3.0-or-later", "SSPL-1.0", "BUSL-1.1"])
def test_blocked_pattern_catches_restrictive_licences(blocked: str) -> None:
    assert _BLOCKED.search(blocked)


@pytest.mark.parametrize(
    "allowed",
    ["MIT", "LGPL-2.1-or-later", "License :: OSI Approved :: GNU Lesser General Public License v3"],
)
def test_blocked_pattern_spares_permissive_and_lgpl(allowed: str) -> None:
    assert not _BLOCKED.search(allowed)


def test_installed_distributions_declare_an_unblocked_licence() -> None:
    reviewed = set(_REVIEW["distributions"])
    problems: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = _canonical(distribution.metadata["Name"])
        if name in reviewed:
            continue
        declared = _declared(distribution)
        if not declared:
            problems[name] = "no licence metadata"
        elif _BLOCKED.search(declared):
            problems[name] = declared
    assert problems == {}


@pytest.mark.parametrize("package", ["app", "engine", "engine/mcp_server"])
@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_package_licence_copies_match_the_root(package: str, name: str) -> None:
    assert (_ROOT / package / name).read_text() == (_ROOT / name).read_text()
