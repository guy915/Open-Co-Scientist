from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from co_scientist.core.config import settings
from co_scientist.core.run_modes import (
    attribute_names,
    clean_string_list,
    criteria_display_strings,
    focus_guidance,
    normalize_run_focus,
    normalize_run_tier,
    setup_guidance,
)
from co_scientist.domains.chat.repository import interviews
from co_scientist.domains.chat.repository import messages as store
from co_scientist.domains.research_state.repository import records
from co_scientist.platform.db.models import MessageRow
from co_scientist.platform.retrieval import run_corpus

if TYPE_CHECKING:
    from co_scientist.core.byok_scope import ByokCredential

# Consumed steering IDs are commit bookkeeping: acknowledgement must share the
# checkpoint that actually honored the guidance.
CONSUMED_STEERING_IDS_OPT = "consumed_steering_ids"


def _apply_capability_opts(initial_opts: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Funded capability flags remain requests; engine checks still enforce
    tools, offline and sandbox availability.
    """
    tier = normalize_run_tier(cfg.get("tier"))
    deep = tier in {"extended", "ultra"}
    strategy = cfg.get("generation_strategy")
    initial_opts.update(
        enable_literature_review_node=(
            bool(cfg.get("enable_literature_review", True))
            and os.getenv("FORCE_LITERATURE_REVIEW") != "0"
        ),
        enable_tool_calling_generation=deep,
        enable_simulation_execution=deep,
        enable_overview_review=deep,
        research_tier=tier,
        enable_meta_review=cfg.get("enable_meta_review", True) is not False,
        generation_strategy=strategy if isinstance(strategy, str) else "",
    )


def _setup_opts_from_cfg(setup: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(setup, dict):
        return {}
    focus = normalize_run_focus(setup.get("focus"))
    return {
        "run_focus_guidance": focus_guidance(focus),
        "run_setup_guidance": setup_guidance(setup),
        # Engine attributes need names; anchored rubric text still reaches
        # prompts through run_setup_guidance.
        "attributes": attribute_names(setup.get("attributes")),
        "constraints": clean_string_list([str(value) for value in setup.get("requirements") or []]),
        # Legacy prose and structured criteria normalize to the same engine
        # string vocabulary.
        "criteria": criteria_display_strings(setup.get("criteria")),
    }


def _lab_constraints_for_run(cfg: dict[str, Any], db_path: str | None) -> list[str]:
    """Interview lab constraints reach feasibility prompts through this
    options boundary, independently of create-request field merging.
    """
    interview_id = cfg.get("interview_id")
    if not interview_id:
        return []
    interview = interviews.get_interview(str(interview_id), db_path=db_path)
    if interview is None:
        return []
    raw = interview["fields"]["lab_constraints"]
    return clean_string_list([str(value) for value in raw])


def _apply_private_sources(
    initial_opts: dict[str, Any], run_id: str, goal: str, db_path: str | None
) -> None:
    private_sources = run_corpus.engine_context_sources(
        records.list_evidence(run_id, db_path=db_path),
        goal,
    )
    if private_sources:
        initial_opts["context_enrichment_sources"] = private_sources
        initial_opts["user_inputs"] = {
            "literature": [str(item["display"]) for item in private_sources]
        }


def build_engine_opts(cfg: dict[str, Any], run_id: str, db_path: str | None) -> dict[str, Any]:
    """Reading steering never acknowledges it; acknowledgement belongs to
    the checkpoint transaction that actually applies it.
    """
    initial_opts = _setup_opts_from_cfg(cfg.get("setup"))
    # Pending steering also schedules high-priority generation, rather than
    # remaining passive initial preferences.
    pending_steering = store.get_pending_steering(run_id, db_path=db_path)
    if pending_steering:
        initial_opts["pending_steering"] = True
        initial_opts[CONSUMED_STEERING_IDS_OPT] = [message.id for message in pending_steering]
    preferences = _steering_preferences(
        str(initial_opts.get("run_setup_guidance") or ""),
        store.list_messages(run_id, db_path=db_path),
    )
    if preferences:
        initial_opts["preferences"] = preferences
    _apply_capability_opts(initial_opts, cfg)
    lab_constraints = _lab_constraints_for_run(cfg, db_path)
    if lab_constraints:
        initial_opts["lab_constraints"] = lab_constraints
    goal = str((cfg.get("setup") or {}).get("goal") or "")
    _apply_private_sources(initial_opts, run_id, goal, db_path)
    return initial_opts


def _resolve_generator_models(offline: bool) -> tuple[str, str | None]:
    if not offline:
        return settings.model_name, settings.supervisor_model_name
    # Import locally after sibling engine discovery, avoiding a hard dependency
    # at app-package import time.
    from co_scientist.platform.llm.offline.llm import DEFAULT_OFFLINE_MODEL

    return DEFAULT_OFFLINE_MODEL, DEFAULT_OFFLINE_MODEL


def _generator_kwargs(
    cfg: dict[str, Any],
    model_name: str,
    supervisor_model_name: str | None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Resolved numeric configuration is durable; BYOK credential material
    never enters run state.
    """
    from co_scientist.orchestration.generator.run_setup import GeneratorOptions

    return {
        "model_name": model_name,
        "max_iterations": int(cfg["max_iterations"]),
        "initial_hypotheses_count": int(cfg["initial_hypotheses_count"]),
        "evolution_max_count": int(cfg["evolution_max_count"]),
        "options": GeneratorOptions(
            supervisor_model_name=supervisor_model_name,
            budget={
                "max_llm_calls": int(cfg["max_llm_calls"]),
                "max_ideas": int(cfg["max_ideas"]),
                "max_matches_per_idea": float(cfg["max_matches_per_idea"]),
                "finalists": int(cfg["finalists"]),
            },
            tournament_pairs=int(cfg["tournament_pairs"]),
            elo_k_factor=int(cfg["k_factor"]),
            # Translate the sole literature-budget knob here rather than
            # persisting another synchronized key.
            literature_review_papers_count=int(cfg["evidence_count"]),
            disable_tools=[] if cfg.get("enable_web_search", True) else ["web_search"],
            api_key=api_key,
        ),
    }


def build_generator(
    generator_cls: Any,
    cfg: dict[str, Any],
    *,
    offline: bool = False,
    byok: ByokCredential | None = None,
) -> Any:
    """A fresh generator isolates each run's tier and model settings;
    validated BYOK runs remain real-backed.
    """
    model_name: str
    supervisor_model_name: str | None
    if byok is not None:
        # Validated worker/supervisor choices remain real-backed.
        model_name = byok.model
        supervisor_model_name = byok.supervisor_model or byok.model
    else:
        model_name, supervisor_model_name = _resolve_generator_models(offline)
    return generator_cls(
        **_generator_kwargs(
            cfg,
            model_name,
            supervisor_model_name,
            api_key=byok.api_key if byok else None,
        )
    )


def _steering_preferences(setup_text: str, messages: list[MessageRow]) -> str | None:
    """Acknowledged steering remains guidance across subsequent checkpoint
    restores.
    """
    steering = [f"- {message.content}" for message in messages if message.kind == "steering"]
    parts = [setup_text] if setup_text else []
    if steering:
        parts.append("User steering guidance:\n" + "\n".join(steering))
    return "\n\n".join(parts) or None
