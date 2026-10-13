from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from co_scientist.core.config import settings
from co_scientist.core.constants import MAX_PLANNING_IDEAS, MAX_PLANNING_K_FACTOR

DEFAULT_K_FACTOR: int = settings.elo_k_factor

DEFAULT_CRITERIA: tuple[dict[str, str], ...] = (
    {"name": "Idea correctness", "value": "Required"},
    {"name": "Idea novelty", "value": "Required"},
    {"name": "Maximize impact", "value": "Yes"},
)


def _named_criterion(item: dict[str, Any]) -> tuple[str, str] | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    return name, str(item.get("value") or "").strip()


def clean_criteria_list(values: list[Any] | None = None) -> list[Any]:
    cleaned: list[Any] = []
    for item in values or []:
        if isinstance(item, dict):
            pair = _named_criterion(item)
            if pair is not None:
                cleaned.append({"name": pair[0], "value": pair[1]})
        else:
            text = str(item).strip()
            if text:
                cleaned.append(text)
    return cleaned


def _criterion_display_line(item: Any) -> str:
    if isinstance(item, dict):
        pair = _named_criterion(item)
        if pair is None:
            return ""
        name, value = pair
        return f"{name}: {value}" if value else name
    return str(item).strip()


def criteria_display_strings(raw_values: Any) -> list[str]:
    if not isinstance(raw_values, list):
        return []
    return [line for item in raw_values if (line := _criterion_display_line(item))]


def _default_criteria() -> list[dict[str, str]]:
    """Return fresh baseline dictionaries so callers cannot mutate another
    run's defaults.
    """
    return [dict(pair) for pair in DEFAULT_CRITERIA]


_SCALE_POINTS: tuple[str, ...] = ("1", "3", "5")

# Goal-agnostic defaults cannot invent goal-derived categorical axes; caller-
# provided categorical attributes remain supported.
DEFAULT_ATTRIBUTES: tuple[dict[str, Any], ...] = (
    {
        "name": "Mechanistic specificity",
        "scale": {
            "1": "Vague or hand-wavy mechanism",
            "3": "Plausible mechanism with some unexplained steps",
            "5": "Precise, causally complete mechanism",
        },
    },
    {
        "name": "Evidence grounding",
        "scale": {
            "1": "Speculative, no literature support",
            "3": "Partially supported by existing evidence",
            "5": "Strongly grounded in established literature",
        },
    },
    {
        "name": "Experimental readiness",
        "scale": {
            "1": "No clear path to testing",
            "3": "Testable with significant additional groundwork",
            "5": "Directly testable with a well-defined validation plan",
        },
    },
)


def _clean_scale(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, str] = {}
    for point in _SCALE_POINTS:
        text = str(raw.get(point) or "").strip()
        if text:
            cleaned[point] = text
    return cleaned


def _clean_values(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [text for item in raw if (text := str(item).strip())]


def _clean_attribute_dict(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    scale = _clean_scale(item.get("scale"))
    if scale:
        return {"name": name, "scale": scale}
    values = _clean_values(item.get("values"))
    if values:
        return {"name": name, "values": values}
    return {"name": name}


def clean_attributes_list(values: list[Any] | None = None) -> list[Any]:
    cleaned: list[Any] = []
    for item in values or []:
        if isinstance(item, dict):
            normalized = _clean_attribute_dict(item)
            if normalized is not None:
                cleaned.append(normalized)
        else:
            text = str(item).strip()
            if text:
                cleaned.append(text)
    return cleaned


def _join_with_or(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} or {values[1]}"
    return ", ".join(values[:-1]) + f", or {values[-1]}"


def _scaled_display_line(name: str, scale: dict[str, str]) -> str:
    anchors = ", ".join(f"{point}: {scale[point]}" for point in _SCALE_POINTS if point in scale)
    return f"{name}: 1-5 scale ({anchors})" if anchors else name


def _categorical_display_line(name: str, values: list[str]) -> str:
    return f"{name} ({_join_with_or(values)})" if values else name


def _dict_attribute_display_line(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    if not name:
        return ""
    scale = item.get("scale")
    if isinstance(scale, dict) and scale:
        return _scaled_display_line(name, scale)
    values = item.get("values")
    if isinstance(values, list) and values:
        return _categorical_display_line(name, [str(v) for v in values])
    return name


def _attribute_display_line(item: Any) -> str:
    if isinstance(item, dict):
        return _dict_attribute_display_line(item)
    return str(item).strip()


def attribute_display_strings(raw_values: Any) -> list[str]:
    if not isinstance(raw_values, list):
        return []
    return [line for item in raw_values if (line := _attribute_display_line(item))]


def attribute_names(raw_values: Any) -> list[str]:
    if not isinstance(raw_values, list):
        return []
    names: list[str] = []
    for item in raw_values:
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else str(item).strip()
        if name:
            names.append(name)
    return names


def _default_attributes() -> list[dict[str, Any]]:
    """Return fresh baseline dictionaries so callers cannot mutate another
    run's defaults.
    """
    fresh: list[dict[str, Any]] = []
    for item in DEFAULT_ATTRIBUTES:
        copy: dict[str, Any] = {"name": item["name"]}
        if "scale" in item:
            copy["scale"] = dict(item["scale"])
        if "values" in item:
            copy["values"] = list(item["values"])
        fresh.append(copy)
    return fresh


DEFAULT_RUN_TIER = "standard"
RUN_TIER_PATTERN = "^(express|standard|extended|ultra)$"
DEFAULT_RUN_FOCUS = "balance"
RUN_FOCUS_VALUES: tuple[str, ...] = (
    "prefer_evidence",
    "balance",
    "prefer_novelty",
    "breakthrough",
)
RUN_FOCUS_PATTERN = "^(" + "|".join(RUN_FOCUS_VALUES) + ")$"

# Direct API calls and demos receive the same client-independent planning
# baseline.
DEFAULT_REQUIREMENTS: tuple[str, ...] = (
    "Prioritize mechanistic novelty, plausibility, and direct testability.",
    "Retrieve broader literature evidence and preserve competing mechanisms.",
    "Use tournament ranking and evolution before final synthesis.",
)

# Call budgets are runaway caps far above measured use (docs/OPERATIONS.md, Run
# efficiency) that bound loops without charging recovery downtime. They also
# gate knowledge-base synthesis and interim overviews.
RUN_TIER_DEFAULTS: dict[str, dict[str, int]] = {
    "express": {
        "initial_hypotheses_count": 4,
        "max_iterations": 1,
        "evolution_max_count": 4,
        "tournament_pairs": 6,
        "evidence_count": 4,
        "max_llm_calls": 1200,
        "max_ideas": 12,
        "max_matches_per_idea": 4,
        "finalists": 3,
    },
    DEFAULT_RUN_TIER: {
        "initial_hypotheses_count": 8,
        "max_iterations": 2,
        "evolution_max_count": 8,
        "tournament_pairs": 12,
        "evidence_count": 8,
        "max_llm_calls": 2500,
        "max_ideas": 32,
        "max_matches_per_idea": 7,
        "finalists": 5,
    },
    "extended": {
        "initial_hypotheses_count": 12,
        "max_iterations": 3,
        "evolution_max_count": 12,
        "tournament_pairs": 20,
        "evidence_count": 12,
        "max_llm_calls": 7000,
        "max_ideas": 60,
        "max_matches_per_idea": 11,
        "finalists": 6,
    },
    "ultra": {
        "initial_hypotheses_count": 16,
        "max_iterations": 4,
        "evolution_max_count": 16,
        "tournament_pairs": 32,
        "evidence_count": 16,
        "max_llm_calls": 14000,
        "max_ideas": 96,
        "max_matches_per_idea": 17,
        "finalists": 8,
    },
}


def planning_limits(tier: str) -> dict[str, int]:
    defaults = RUN_TIER_DEFAULTS[tier]
    return {
        **defaults,
        "initial_hypotheses_count": defaults["max_ideas"],
        "evolution_max_count": defaults["max_ideas"],
        "max_iterations": defaults["max_iterations"] * 2,
        "k_factor": MAX_PLANNING_K_FACTOR,
    }


def validate_planning_config(config: dict[str, Any], tier: str) -> None:
    for key, maximum in planning_limits(tier).items():
        value = config.get(key)
        numeric_type = type(value) is int or (
            key == "max_matches_per_idea" and type(value) is float
        )
        if value is not None and (not numeric_type or not 0 <= value <= maximum):
            raise ValueError(f"{key} must be an integer between 0 and {maximum} for tier {tier}")
    if config.get("initial_hypotheses_count", 0) > config.get("max_ideas", MAX_PLANNING_IDEAS):
        raise ValueError("initial_hypotheses_count exceeds max_ideas")


# Unknown vocabulary normalizes to the default so loosely validated callers
# remain readable.
def normalize_run_tier(tier: str | None = None) -> str:
    if tier in RUN_TIER_DEFAULTS:
        return tier
    return DEFAULT_RUN_TIER


def normalize_run_focus(focus: str | None = None) -> str:
    if focus in RUN_FOCUS_VALUES:
        return focus
    return DEFAULT_RUN_FOCUS


def clean_string_list(values: list[str] | None = None) -> list[str]:
    return [value.strip() for value in values or [] if value.strip()]


@dataclasses.dataclass(frozen=True)
class PlanningLists:
    requirements: list[str] | None = None
    attributes: list[Any] | None = None
    criteria: list[Any] | None = None


def setup_config(
    *,
    research_goal: str,
    lists: PlanningLists | None = None,
    focus: str | None = None,
    tier: str | None = None,
) -> dict[str, Any]:
    """Direct API calls and demos retain a client-independent baseline when
    planning lists are omitted.
    """
    lists = lists or PlanningLists()
    return {
        "goal": research_goal.strip(),
        "requirements": clean_string_list(lists.requirements) or list(DEFAULT_REQUIREMENTS),
        "attributes": clean_attributes_list(lists.attributes) or _default_attributes(),
        "criteria": clean_criteria_list(lists.criteria) or _default_criteria(),
        "focus": normalize_run_focus(focus),
        "tier": normalize_run_tier(tier),
    }


def focus_guidance(focus: str | None) -> str:
    focus = normalize_run_focus(focus)
    if focus == "prefer_evidence":
        return (
            "Prefer evidence: prioritize literature-grounded, feasible, and "
            "well-supported hypotheses. Penalize speculative leaps unless "
            "they include a clear validation path."
        )
    if focus == "prefer_novelty":
        return (
            "Prefer novelty: reward hypotheses that introduce distinct "
            "mechanisms or experimental angles while preserving scientific "
            "plausibility and testability."
        )
    if focus == "breakthrough":
        return (
            "Breakthrough: actively explore high-impact, high-risk ideas. "
            "Surface uncertainties explicitly instead of over-penalizing "
            "speculative but testable mechanisms."
        )
    return "Balance: weigh evidence support, novelty, feasibility, and testability evenly."


def _setup_field_lines(title: str, values: list[str]) -> list[str]:
    if not values:
        return []
    lines = [f"- {title}:"]
    lines.extend(f"  - {value}" for value in values)
    return lines


# Attributes and criteria support legacy prose and structured shapes;
# requirements remain plain strings.
_SETUP_DISPLAY_COERCIONS: dict[str, Callable[[Any], list[str]]] = {
    "attributes": attribute_display_strings,
    "criteria": criteria_display_strings,
}


def _setup_field_values(setup: dict[str, Any], key: str) -> list[str]:
    coerce = _SETUP_DISPLAY_COERCIONS.get(key)
    if coerce is not None:
        return coerce(setup.get(key))
    return clean_string_list([str(v) for v in setup.get(key) or []])


def setup_guidance(setup: dict[str, Any] | None) -> str:
    if not isinstance(setup, dict):
        return ""
    focus = focus_guidance(str(setup.get("focus") or ""))
    tier = normalize_run_tier(str(setup.get("tier") or ""))
    lines = [
        "Run setup:",
        f"- Focus: {focus}",
        f"- Run type: {tier}",
    ]
    for title, key in (
        ("Requirements", "requirements"),
        ("Attributes", "attributes"),
        ("Criteria", "criteria"),
    ):
        lines.extend(_setup_field_lines(title, _setup_field_values(setup, key)))
    return "\n".join(lines)


def _apply_numeric_override(base: dict[str, Any], key: str, raw_value: Any) -> None:
    """Overrides may raise tier baselines but never undo a selected larger
    tier.
    """
    if raw_value is None:
        return
    try:
        value = int(raw_value)
    except (ValueError, TypeError):
        return
    base[key] = max(base[key], value) if key in base else value


# Structured mode and BYOK fields must survive round-trips; numeric coercion
# would silently discard them.
_VERBATIM_OVERRIDES = {
    "setup",
    "byok_provider",
    "generation_strategy",
    "discovery",
}
_OVERRIDE_COERCIONS: dict[str, Callable[[Any], Any]] = {
    "focus": lambda value: normalize_run_focus(value if isinstance(value, str) else None),
    "llm_backend": lambda value: value if value in ("offline", "real") else None,
    "enable_literature_review": bool,
    "enable_web_search": bool,
    "enable_meta_review": bool,
}


def _apply_run_config_override(base: dict[str, Any], key: str, raw_value: Any) -> None:
    if key == "tier":
        return
    if key in _VERBATIM_OVERRIDES:
        base[key] = raw_value
    elif coerce := _OVERRIDE_COERCIONS.get(key):
        base[key] = coerce(raw_value)
    else:
        _apply_numeric_override(base, key, raw_value)


def _resolve_tier_override(overrides: dict[str, Any] | None) -> str:
    tier = None
    if overrides:
        tier = overrides.get("tier")
        setup = overrides.get("setup")
        if tier is None and isinstance(setup, dict):
            tier = setup.get("tier")
    return normalize_run_tier(tier if isinstance(tier, str) else None)


def _ensure_focus_default(base: dict[str, Any]) -> None:
    if "focus" in base:
        return
    setup = base.get("setup")
    if isinstance(setup, dict):
        base["focus"] = normalize_run_focus(setup.get("focus"))
    else:
        base["focus"] = DEFAULT_RUN_FOCUS


def resolved_run_config(
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tier = _resolve_tier_override(overrides)
    # Copy tier defaults so one run's overrides cannot mutate subsequent runs.
    base: dict[str, Any] = dict(RUN_TIER_DEFAULTS[tier])
    if overrides:
        validate_planning_config(overrides, tier)
        for key, raw_value in overrides.items():
            _apply_run_config_override(base, key, raw_value)
    base["tier"] = tier
    _ensure_focus_default(base)
    base.setdefault("k_factor", DEFAULT_K_FACTOR)
    base.setdefault("enable_literature_review", True)
    base.setdefault("llm_backend", None)
    base.setdefault("enable_web_search", True)
    base.setdefault("enable_meta_review", True)
    validate_planning_config(base, tier)
    return base


__all__ = [
    "_default_attributes",
    "_default_criteria",
    "attribute_display_strings",
    "attribute_names",
    "clean_criteria_list",
    "criteria_display_strings",
]
