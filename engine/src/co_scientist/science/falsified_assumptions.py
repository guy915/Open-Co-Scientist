from __future__ import annotations

import logging
from typing import Any

from co_scientist.core.constants import truncate
from co_scientist.domains.research_state.models import Hypothesis

logger = logging.getLogger(__name__)


# Bound falsified-probe guidance so it does not crowd ideation out of the
# prompt.


MAX_FALSIFIED_ASSUMPTION_LINES = 6

_PROBE_FIELD_CHARS = 240


def _probe_explicitly_falsified(probe: dict[str, Any]) -> bool | None:
    holds = probe.get("assumption_holds")
    if isinstance(holds, bool):
        return not holds
    return None


def _probe_admitted(probe: dict[str, Any], verdict: str | None) -> bool:
    """Fundamental failures kill the idea rather than seed assumption
    generation; explicit assumption_holds overrides inferred verdicts."""
    if probe.get("assumption_is_fundamental"):
        return False
    explicit = _probe_explicitly_falsified(probe)
    if explicit is not None:
        return explicit
    return verdict == "weakened"


def _format_probe_line(probe: dict[str, Any]) -> str:
    question = truncate(str(probe.get("question") or "").strip(), _PROBE_FIELD_CHARS)
    answer = truncate(str(probe.get("answer") or "").strip(), _PROBE_FIELD_CHARS)
    if answer:
        return f"{question} -- finding: {answer}"
    return question


def _falsified_lines_for_hypothesis(hypothesis: Hypothesis) -> list[str]:
    probes = hypothesis.deep_verification_probes
    if not probes:
        return []
    verdict = hypothesis.deep_verification_verdict
    return [_format_probe_line(probe) for probe in probes if _probe_admitted(probe, verdict)]


def falsified_nonfundamental_assumptions(
    hypotheses: list[Hypothesis],
) -> list[str]:
    lines: list[str] = []
    for hypothesis in hypotheses:
        lines.extend(_falsified_lines_for_hypothesis(hypothesis))
        if len(lines) >= MAX_FALSIFIED_ASSUMPTION_LINES:
            logger.info(
                "Falsified-assumption guidance capped at %s lines",
                MAX_FALSIFIED_ASSUMPTION_LINES,
            )
            return lines[:MAX_FALSIFIED_ASSUMPTION_LINES]
    return lines


def build_falsified_assumptions_section(
    hypotheses: list[Hypothesis] | None,
) -> str:
    lines = falsified_nonfundamental_assumptions(hypotheses or [])
    if not lines:
        return ""
    bullets = "".join(f"- {line}\n" for line in lines)
    return (
        "## Assumptions Verified Incorrect (avoid or rework)\n\n"
        "Verification in this run already found these non-fundamental"
        " assumptions incorrect. Do not build new hypotheses on them;"
        " avoid them or rework around them:\n"
        f"{bullets}\n"
    )
