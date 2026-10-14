from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from litellm.exceptions import APIError

import co_scientist.platform.llm as llm
from co_scientist.core.exceptions import (
    LLMTimeoutError,
)
from co_scientist.orchestration.task_runtime import next_task_type
from co_scientist.orchestration.workflow_topology import (
    WORKFLOW_ROUTES,
)
from co_scientist.science.meta_review import meta_review as mr
from co_scientist.science.meta_review import research_overview as ro
from co_scientist.science.proximity import proximity as px
from tests._state import (
    decision_states,
    make_hypothesis,
    make_review,
    make_state,
)

# Node keys persist in tasks and checkpoints; renaming requires a data
# migration.


_TIMEOUT = LLMTimeoutError("LLM call to openrouter/minimax/minimax-m3:free exceeded 600.0s")
_UPSTREAM = APIError(
    status_code=500,
    message="OpenrouterException - Upstream error from Nvidia: overloaded",
    llm_provider="openrouter",
    model="minimax/minimax-m3:free",
)

# The two the durable worker answers itself -- a park waits for a clock and
# a spent ceiling terminates the run, so neither may be swallowed here.


def _raiser(error: Exception) -> Any:

    async def _call(*_: Any, **__: Any) -> dict[str, Any]:
        raise error

    return _call


def _overview_state(**overrides: Any) -> Any:
    return make_state(
        hypotheses=[make_hypothesis(text="H", elo_rating=1700)],
        research_goal="g",
        supervisor_model_name="test/model",
        meta_review={},
        articles=[],
        **overrides,
    )


def _reviewed_state() -> Any:
    hypothesis = make_hypothesis(text="H")
    hypothesis.reviews = [make_review()]
    return make_state(
        hypotheses=[hypothesis],
        research_goal="g",
        supervisor_model_name="test/model",
    )


def _pair_state() -> Any:
    return make_state(
        hypotheses=[make_hypothesis(text="A"), make_hypothesis(text="B")],
        research_goal="g",
        model_name="test/model",
    )


# (module, node, state builder, name recorded in degraded_nodes)
_NODE_CASES = [
    pytest.param(
        ro,
        ro.research_overview_node,
        _overview_state,
        "research_overview",
        id="research_overview",
    ),
    pytest.param(
        mr,
        mr.meta_review_node,
        _reviewed_state,
        "meta_review",
        id="meta_review",
    ),
    pytest.param(
        px,
        px.proximity_node,
        _pair_state,
        "proximity_analysis",
        id="proximity",
    ),
]


@pytest.mark.parametrize(("module", "node", "build_state", "degraded_name"), _NODE_CASES)
@pytest.mark.parametrize("error", [_TIMEOUT, _UPSTREAM])
@pytest.mark.parametrize("retries_remain", [None, False, True], ids=["graph", "last", "remain"])
async def test_provider_failure_degrades_only_on_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    node: Any,
    build_state: Any,
    degraded_name: str,
    error: Exception,
    retries_remain: bool | None,
) -> None:
    """Use remaining durable retries before settling for a blank report
    section; raising after the last one would settle the run without it."""
    monkeypatch.setattr(module, "call_llm_json", _raiser(error))
    state = build_state()
    if retries_remain is not None:
        state["durable_retries_remain"] = retries_remain

    if retries_remain:
        with pytest.raises(type(error)):
            await node(state)
        assert state.get("degraded_nodes", []) == []
    else:
        await node(state)
        assert state["degraded_nodes"] == [degraded_name]


@pytest.mark.parametrize("node", sorted(WORKFLOW_ROUTES))
def test_a_safety_halt_ends_the_durable_path_from_every_node(
    node: str,
) -> None:
    for state in decision_states():
        halted = make_state(**{**state, "safety_blocked": True})
        assert next_task_type(node, halted) is None


def test_no_module_imports_the_interface_it_implements() -> None:
    modules = _modules()
    for name, path in modules.items():
        assert "" not in _runtime_imports(path, set(modules)), name


def test_imports_only_point_downward() -> None:
    modules = _modules()
    for name, path in modules.items():
        for imported in _runtime_imports(path, set(modules)) - {""}:
            if (name.split(".")[0], imported) in _ALLOWED_UPWARD:
                continue
            assert _layer(imported) <= _layer(name), f"{name} -> {imported}"


def test_module_level_imports_form_no_cycle() -> None:
    modules = _modules()
    graph = {name: _runtime_imports(path, set(modules)) - {""} for name, path in modules.items()}
    for name in graph:
        assert name not in _reachable(graph, name), name


def _layer(module: str) -> int:
    return _LAYERS.index(module.split(".")[0])


def _runtime_imports(path: pathlib.Path, modules: set[str]) -> set[str]:
    found: set[str] = set()
    for node in _runtime_nodes(ast.parse(path.read_text()).body):
        found |= _targets(node, modules)
    return found


def _modules() -> dict[str, pathlib.Path]:
    return {
        ".".join(path.relative_to(_ROOT).with_suffix("").parts): path
        for path in _ROOT.rglob("*.py")
        if path.name != "__init__.py"
    }


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    todo = list(graph.get(start, ()))
    while todo:
        module = todo.pop()
        if module not in seen:
            seen.add(module)
            todo.extend(graph.get(module, ()))
    return seen


_ALLOWED_UPWARD = {("telemetry", "request.response")}


# Telemetry reads request.response; request.completion records into telemetry.
# That request-layer edge is the explicit layering exception.
_LAYERS = (
    "scoped_loop",
    "profile",
    "roles",
    "values",
    "admission",
    "structured",
    "telemetry",
    "routing",
    "request",
    "precall",
    "attempts",
    "tool_effects",
    "tools",
    "call",
    "offline",
    "execution_policy",
    "process_mode",
    "provider_usage",
    "offline_guard",
    "llm_scope",
    "llm_request",
)


def _runtime_nodes(body: list[ast.stmt]) -> Iterator[ast.ImportFrom]:
    for node in body:
        if isinstance(node, ast.If) and "TYPE_CHECKING" not in ast.dump(node.test):
            yield from _runtime_nodes(node.body + node.orelse)
        elif isinstance(node, ast.ImportFrom):
            yield node


def _targets(node: ast.ImportFrom, modules: set[str]) -> set[str]:
    module = node.module or ""
    if not module.startswith(_PACKAGE):
        return set()
    base = module.removeprefix(_PACKAGE).removeprefix(".")
    joined = {f"{base}.{a.name}" if base else a.name for a in node.names}
    return {j if j in modules else base for j in joined}


_ROOT = pathlib.Path(llm.__file__).parent


_PACKAGE = "co_scientist.platform.llm"
