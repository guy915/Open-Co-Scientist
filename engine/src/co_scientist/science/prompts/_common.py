from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from co_scientist.core.constants import truncate


@dataclass(frozen=True)
class PromptRunContext:
    supervisor_guidance: dict[str, Any] | None = None
    meta_review: dict[str, Any] | None = None
    tool_registry: Any | None = None
    run_setup_guidance: str | None = None
    run_focus_guidance: str | None = None
    preferences: str | None = None
    criteria: list[str] | None = None


@dataclass(frozen=True)
class PromptSections:
    """None leaves a placeholder unresolved; an empty string explicitly
    suppresses its section.
    """

    supervisor_guidance: str | None = None
    meta_review_context: str | None = None
    run_guidance: str | None = None


def _run_guidance_section(context: PromptRunContext) -> str:
    return _format_run_guidance(context.run_setup_guidance, context.run_focus_guidance)


def _format_bullet_section(
    header: str, items: list[Any], format_item: Callable[[Any], str] = str
) -> str:
    if not items:
        return ""
    lines = [f"**{header}:**\n"]
    lines.extend(f"- {format_item(item)}\n" for item in items)
    lines.append("\n")
    return "".join(lines)


def _format_recommendation(rec: Any) -> str:
    if isinstance(rec, dict):
        return str(rec.get("recommendation", str(rec)))
    return str(rec)


def _format_connection(connection: Any) -> str:
    """Use labels rather than echoing full hypotheses into every prompt."""
    if not isinstance(connection, dict):
        return str(connection)
    opportunity = str(connection.get("synthesis_opportunity") or "").strip()
    kind = str(connection.get("connection_type") or "").strip()
    if opportunity and kind:
        return f"{kind}: {opportunity}"
    return opportunity or kind or str(connection)


def _meta_review_closing(include_coverage_sections: bool) -> str:
    closing = "Use these insights to provide more informed and consistent"
    if include_coverage_sections:
        return closing + (
            " reviews, and -- when generating new hypotheses -- to"
            " explore directions distinct from the areas already covered"
            " above.\n"
        )
    return closing + " reviews.\n"


# State uses common_strengths/common_weaknesses rather than the raw schema keys.
def _format_meta_review_context(
    meta_review: dict[str, Any] | None,
    *,
    include_coverage_sections: bool = True,
) -> str:
    """Coverage cues bias the sticky novelty gate against refinements.
    Pool-wide synthesis still includes coverage.
    """
    if not meta_review or not isinstance(meta_review, dict):
        return ""

    sections = [
        "## Meta-Review Context\n",
        "The following insights were synthesized from previous reviews of all hypotheses:\n\n",
        _format_bullet_section(
            "Common Strengths Across Hypotheses",
            meta_review.get("common_strengths", []),
        ),
        _format_bullet_section(
            "Common Weaknesses to Watch For",
            meta_review.get("common_weaknesses", []),
        ),
    ]
    if include_coverage_sections:
        sections.append(
            _format_bullet_section(
                "Research Areas Already Covered (Recurring Themes)",
                meta_review.get("emerging_themes", []),
            )
        )
    sections.append(
        _format_bullet_section(
            "Strategic Recommendations",
            meta_review.get("strategic_recommendations", []),
            _format_recommendation,
        )
    )
    if include_coverage_sections:
        sections.append(
            _format_bullet_section(
                "Open Directions Flagged for Further Exploration",
                meta_review.get("potential_connections", []),
                _format_connection,
            )
        )
    sections.append(_meta_review_closing(include_coverage_sections))
    return "".join(sections)


def _format_run_guidance(
    run_setup_guidance: str | None = None, run_focus_guidance: str | None = None
) -> str:
    """Absent guidance leaves the original prompt unchanged."""
    sections = []
    if run_setup_guidance:
        sections.append("## Run Setup Guidance\n")
        sections.append(run_setup_guidance.strip())
        sections.append("\n")
    if run_focus_guidance:
        sections.append("## Run Focus Guidance\n")
        sections.append(run_focus_guidance.strip())
        sections.append("\n")
    return "\n".join(sections).strip()


def format_lab_constraints_section(
    lab_constraints: list[str] | None,
) -> str:
    if not lab_constraints:
        return ""
    items = "\n".join(f"- {constraint}" for constraint in lab_constraints)
    return (
        "## Scientist's Lab Constraints\n\n"
        "The scientist supplied these lab constraints. Respect them when"
        " proposing experiments and judging feasibility: an experiment is"
        " feasible only if it can be run within these constraints, and a"
        " proposal that requires equipment, model systems, or resources"
        " excluded here must say so and offer an alternative that fits.\n\n"
        f"{items}\n"
    )


def _format_bullet_list(items: list[str] | None, *, truncate_chars: int | None = None) -> str:
    if not items:
        return "None provided"
    if truncate_chars is not None:
        items = [truncate(item, truncate_chars) for item in items]
    return "\n".join(f"- {item}" for item in items)


def _csv_value(value: Any) -> str:
    """Unenforced JSON may supply a bare string; treat it as one item, not
    characters.
    """
    return ", ".join(value) if isinstance(value, list) else str(value)


def _guidance_items(value: Any) -> list[Any]:
    """Unenforced JSON may supply a bare string; treat it as one item, not
    characters.
    """
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _format_csv_list(items: list[str] | None) -> str:
    return ", ".join(items) if items else "None provided"
