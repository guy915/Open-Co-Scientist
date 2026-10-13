"""Identity hashing avoids execution imports; snapshots load app/engine
dependencies only when needed.
"""

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_POLICY_FILES = (
    "core/constants/__init__.py",
    "platform/llm/values.py",
    "platform/llm/profile/__init__.py",
    "platform/llm/request/backend.py",
    "platform/llm/request/completion.py",
    "platform/llm/admission/free_policy.py",
    "platform/llm/request/thinking.py",
    "platform/llm/attempts/escalation.py",
)


def identity_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def request_policy() -> dict[str, str]:
    engine = _ROOT / "engine" / "src" / "co_scientist"
    return {
        name: hashlib.sha256((engine / name).read_bytes()).hexdigest() for name in _POLICY_FILES
    }


def validate_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("comparison identity is missing or unsupported")
    contents = {key: item for key, item in value.items() if key != "digest"}
    if value.get("digest") != identity_digest(contents):
        raise ValueError("comparison identity digest does not match its contents")
    return value


def _configured_models() -> dict[str, str | None]:
    from co_scientist.core.config import settings

    return {
        "worker": settings.model_name,
        "supervisor": settings.supervisor_model_name or settings.model_name,
        "chat": settings.chat_model_name or settings.model_name,
        "safety": settings.semantic_safety_model,
        "claim_verifier": settings.claim_verifier_model or settings.model_name,
    }


def _tools_identity() -> dict[str, str | None]:
    return {"mcp_endpoint_sha256": identity_digest(os.getenv("MCP_SERVER_URL"))}


def _baseline_config(goal: str, tier: str) -> dict[str, Any]:
    from co_scientist.core.run_modes import resolved_run_config, setup_config

    baseline: dict[str, Any] = resolved_run_config(
        {"setup": setup_config(research_goal=goal, tier=tier), "tier": tier}
    )
    return baseline


def _model_policy() -> dict[str, Any]:
    from co_scientist.platform.llm import deepseek_thinking_extra_body

    models = _configured_models()
    policy = request_policy()
    return {
        "configured_models": models,
        "routing": {
            model: {
                "reasoning_enabled": deepseek_thinking_extra_body(model),
                "reasoning_disabled": deepseek_thinking_extra_body(model, enabled=False),
            }
            for model in sorted({m for m in models.values() if m})
        },
        "request_policy_files": policy,
        "request_policy_sha256": identity_digest(policy),
    }


def arm_identity(
    goal: str,
    config: dict[str, Any],
    backend: str,
) -> dict[str, Any]:
    """Hash paths/endpoints because they may contain credentials; declared
    controls do not prove served evidence.
    """
    from co_scientist.core.run_modes import RUN_TIER_DEFAULTS

    tier = config["tier"]
    manifest = {
        "version": 1,
        "goal_sha256": hashlib.sha256(goal.encode()).hexdigest(),
        "backend": backend,
        "resolved_config": config,
        "baseline_config": _baseline_config(goal, tier),
        "tier_fields": sorted(RUN_TIER_DEFAULTS[tier]),
        **_model_policy(),
        "tools": _tools_identity(),
        "execution_environment": {
            name: os.getenv(name)
            for name in (
                "COSCIENTIST_REQUIRE_FREE_MODELS",
                "FORCE_LITERATURE_REVIEW",
                "PYTHONHASHSEED",
            )
        },
    }
    if os.getenv("COSCIENTIST_TEST_DOUBLE") is not None:
        manifest["execution_environment"]["COSCIENTIST_TEST_DOUBLE"] = os.getenv(
            "COSCIENTIST_TEST_DOUBLE"
        )
    # Snapshot mutable config/routing maps before placing this in run.config.
    frozen: dict[str, Any] = json.loads(json.dumps(manifest))
    return {**frozen, "digest": identity_digest(frozen)}


def validate_stored_arm(
    run_id: str,
    db_path: str,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from co_scientist.platform.db import runs

    run = runs.get_run(run_id, db_path=db_path)
    if run is None:
        raise ValueError("comparison run is missing")
    identity = validate_identity(run.config.get("evaluation_identity"))
    if expected is not None and identity != expected:
        raise ValueError("comparison identity changed during execution")
    config = {key: value for key, value in run.config.items() if key != "evaluation_identity"}
    current = arm_identity(run.research_goal, config, str(run.llm_backend))
    if current != identity:
        raise ValueError("comparison controls changed; rerun both arms")
    return identity


PANEL_FILES = {
    "citation_entailment": "citation_eval.py",
    "citation_usefulness": "citation_usefulness_eval.py",
}


def _identity(
    panel: str,
    dataset: dict[str, Any],
    model: str,
    live: bool,
) -> dict[str, Any]:
    from co_scientist.platform.llm import deepseek_thinking_extra_body

    source = Path(__file__).parent / PANEL_FILES[panel]
    fields = {
        "version": 1,
        "kind": "panel",
        "panel": panel,
        "dataset": dataset,
        "model": model,
        "execution_mode": "live_requested" if live else "offline",
        "evaluator_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "request_policy_files": request_policy(),
        "routing": {
            "reasoning_enabled": deepseek_thinking_extra_body(model),
            "reasoning_disabled": deepseek_thinking_extra_body(model, enabled=False),
        }
        if live
        else {},
    }
    frozen: dict[str, Any] = json.loads(json.dumps(fields))
    return {**frozen, "digest": identity_digest(frozen)}


@contextmanager
def capture_panel(
    panel: str,
    dataset: dict[str, Any],
    model: str,
    *,
    live: bool,
) -> Iterator[dict[str, Any]]:
    from evaluations._usage_evidence import capture_usage

    identity = _identity(panel, dataset, model, live)
    with capture_usage(panel, live=live) as evidence:
        evidence["evaluation_identity"] = identity
        yield evidence
        if _identity(panel, dataset, model, live) != identity:
            raise ValueError("comparison panel inputs changed during execution")
