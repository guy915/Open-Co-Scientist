from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import get_args

import pytest
from co_scientist.api.contracts.common import RunEventActivity
from co_scientist.api.contracts.generate import (
    API_DIR,
    contracts,
    generated_files,
)
from co_scientist.orchestration.repository.events import ACTIVITY_VALUES

_ENGINE_DIR = Path(__file__).resolve().parents[2] / "engine/src/co_scientist"
_INTERVIEWS_DIR = _ENGINE_DIR / "api/interviews"
_INTERVIEW_MODULES = {path.stem for path in _INTERVIEWS_DIR.glob("*.py")} - {"__init__"}


def _layering_imports(path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "co_scientist":
                modules.extend(f"co_scientist.{alias.name}" for alias in node.names)
            else:
                modules.append(module)
    return modules


@pytest.mark.parametrize(
    "source",
    [
        _ENGINE_DIR / "api/operator_access.py",
        _ENGINE_DIR / "domains/documents/staged.py",
        _ENGINE_DIR / "api/documents_access.py",
        _INTERVIEWS_DIR / "turns.py",
    ],
)
def test_shared_services_do_not_import_endpoint_owners(source: Path) -> None:
    forbidden = {
        "co_scientist.main",
        "co_scientist.api.documents",
        "co_scientist.api.logs_api",
        "co_scientist.api.diagnostics_api",
    }
    assert forbidden.isdisjoint(_layering_imports(source)), source


@pytest.mark.parametrize(
    "source",
    ["turns.py", "stream.py"],
)
def test_interview_modules_do_not_reach_into_router_facade(source: str) -> None:
    path = _INTERVIEWS_DIR / source
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "co_scientist.api.interviews":
            assert {alias.name for alias in node.names} <= _INTERVIEW_MODULES, path
        if isinstance(node, ast.Import):
            assert all(alias.name != "co_scientist.api.interviews" for alias in node.names), path


_ROOT = Path(__file__).resolve().parents[2]


def _boundaries_imports(path: Path) -> list[tuple[int, str, list[str]]]:
    imports: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            imports.append((node.lineno, node.module or "", [a.name for a in node.names]))
        elif isinstance(node, ast.Import):
            imports.extend((node.lineno, a.name, []) for a in node.names)
    return imports


def _is_private_engine_import(module: str, names: list[str]) -> bool:
    engine = module == "co_scientist" or module.startswith("co_scientist.")
    private = any(name.startswith("_") for name in [*module.split("."), *names])
    return engine and private


def _engine_private_imports(paths: list[Path]) -> list[str]:
    return [
        f"{path}:{line}: {module}: {', '.join(names)}"
        for path in paths
        for line, module, names in _boundaries_imports(path)
        if _is_private_engine_import(module, names)
    ]


def test_composition_root_consumes_public_engine_operations() -> None:
    violations = _engine_private_imports([_ENGINE_DIR / "main.py"])
    assert not violations, "Private engine imports:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("owner", "coordinators"),
    [
        ("generation/operations.py", {"generation.generate"}),
        ("ranking/operations.py", {"ranking.ranking"}),
        (
            "reflection/deep_verification.py",
            {
                "reflection.deep_verification",
                "reflection.comprehensive_reflection",
                "reflection.reflection",
                "reflection.review",
            },
        ),
        (
            "evolution/operations.py",
            {"evolution.evolve", "evolution.evolve_prompt"},
        ),
    ],
)
def test_shared_operations_do_not_import_graph_coordinators(
    owner: str, coordinators: set[str]
) -> None:
    agent_dir = _ROOT / "engine" / "src" / "co_scientist" / "science"
    forbidden = {f"co_scientist.science.{name}" for name in coordinators}
    modules = {module for _, module, _ in _boundaries_imports(agent_dir / owner)}
    assert forbidden.isdisjoint(modules), (owner, forbidden & modules)


def _tokens(source: str) -> list[str]:
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r",\s*}", "}", source)
    source = re.sub(r"([=:])\s*\|", r"\1", source)
    tokens = re.findall(r""""[^"\n]*"|'[^'\n]*'|[\w$]+|[^\s]""", source)
    return [json.dumps(t[1:-1]) if t.startswith(("'", '"')) else t for t in tokens]


def test_frontend_wire_types_are_generated_from_backend_contracts() -> None:
    generated = generated_files()
    for name, expected in generated.items():
        assert _tokens((API_DIR / name).read_text()) == _tokens(expected), (
            f"Regenerate {name}: cd app && python -m co_scientist.api.contracts.generate"
        )
    exports = set(
        re.findall(
            r"export\s+type\s+\*\s+from\s+['\"](\./wire_[\w]+)['\"]",
            (API_DIR / "runs.ts").read_text(),
        )
    )
    assert exports == {f"./{name.removesuffix('.ts')}" for name in generated}
    contract_names = {name for group in contracts().values() for name in group}
    for path in API_DIR.glob("*.ts"):
        if path.name.startswith("wire_") or path.name.endswith(".test.ts"):
            continue
        source = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(), flags=re.S)
        declarations = set(
            re.findall(
                r"\b(?:interface|type|class|enum)\s+([A-Za-z_$][\w$]*)",
                source,
            )
        )
        assert contract_names.isdisjoint(declarations), (
            path.name,
            contract_names & declarations,
        )


def test_closed_event_vocabulary_matches_the_store() -> None:
    assert set(get_args(RunEventActivity)) == ACTIVITY_VALUES
