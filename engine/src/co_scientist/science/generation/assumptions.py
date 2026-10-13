from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from co_scientist.core.constants import (
    DEEP_HYPOTHESIS_MAX_TOKENS,
    DEFAULT_MAX_TOKENS,
    LOW_TEMPERATURE,
    MEDIUM_TEMPERATURE,
)
from co_scientist.domains.research_state.models import GenerationMethod, Hypothesis
from co_scientist.domains.research_state.state import WorkflowState
from co_scientist.platform.llm import (
    CompletionSpec,
    LLMCallOptions,
    call_llm_json,
)
from co_scientist.science.citations import (
    ReferenceIndex,
    hypothesis_from_llm_output,
)
from co_scientist.science.falsified_assumptions import build_falsified_assumptions_section
from co_scientist.science.generation.expansion_research import (
    build_expansion_section,
)
from co_scientist.science.prompts._common import (
    _format_meta_review_context,
    format_lab_constraints_section,
)
from co_scientist.science.prompts.generation_draft import (
    _build_citation_reference_section,
)
from co_scientist.science.prompts.loading import load_prompt_with_schema

logger = logging.getLogger(__name__)


ASSUMPTION_TREE_MAX_TOP = 6
ASSUMPTION_TREE_MAX_LOAD_BEARING = 3
ASSUMPTION_TREE_MAX_SUB_PER_PARENT = 3


@dataclass
class _AssumptionNode:
    text: str
    load_bearing: bool = False
    sub_assumptions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _AssumptionCallParams:
    prompt_name: str
    max_tokens: int
    temperature: float


# Decomposition needs fidelity; final ideation needs diversity.


_TREE_LEVEL_PARAMS = _AssumptionCallParams(
    prompt_name="generation_assumption_tree",
    max_tokens=DEFAULT_MAX_TOKENS,
    temperature=LOW_TEMPERATURE,
)
_SUB_LEVEL_PARAMS = _AssumptionCallParams(
    prompt_name="generation_assumption_sub",
    max_tokens=DEFAULT_MAX_TOKENS,
    temperature=LOW_TEMPERATURE,
)
# Final ideation needs full mechanism, prediction and experiment depth, not a
# shallow token budget.


_FINAL_LEVEL_PARAMS = _AssumptionCallParams(
    prompt_name="generation_assumptions",
    max_tokens=DEEP_HYPOTHESIS_MAX_TOKENS,
    temperature=MEDIUM_TEMPERATURE,
)


def _resolve_assumptions_context(
    reference_index: ReferenceIndex | None,
    articles_with_reasoning: str | None,
) -> tuple[str, dict[str, dict[str, Any]], str]:
    """Literature prose requires a nonempty reference index so stale degraded
    summaries cannot appear grounded."""
    if reference_index is not None and not reference_index.is_empty():
        reference_text = reference_index.text
        sources: dict[str, dict[str, Any]] = reference_index.sources
        has_references = True
    else:
        reference_text = ""
        sources = {}
        has_references = False

    literature_context = (
        f"Relevant findings from the literature review:\n{articles_with_reasoning}\n"
        if has_references and articles_with_reasoning
        else ""
    )
    return reference_text, sources, literature_context


def _parse_top_assumptions(response: dict[str, Any]) -> list[_AssumptionNode]:
    nodes: list[_AssumptionNode] = []
    for entry in response.get("assumptions") or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("assumption") or "").strip()
        if not text:
            continue
        nodes.append(_AssumptionNode(text=text, load_bearing=bool(entry.get("load_bearing"))))
        if len(nodes) >= ASSUMPTION_TREE_MAX_TOP:
            break
    return nodes


def _select_parents(nodes: list[_AssumptionNode]) -> list[_AssumptionNode]:
    parents = [node for node in nodes if node.load_bearing]
    return parents[:ASSUMPTION_TREE_MAX_LOAD_BEARING]


def _parse_sub_assumptions(response: dict[str, Any], parents: list[_AssumptionNode]) -> None:
    """Wrap malformed parent indices deterministically so fixed-index offline
    fillers still produce a usable tree."""
    if not parents:
        return
    for entry in response.get("parents") or []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("parent_index")
        if not isinstance(index, int):
            continue
        parent = parents[index % len(parents)]
        subs = [
            str(sub).strip() for sub in (entry.get("sub_assumptions") or []) if str(sub).strip()
        ]
        remaining = ASSUMPTION_TREE_MAX_SUB_PER_PARENT - len(parent.sub_assumptions)
        parent.sub_assumptions.extend(subs[: max(0, remaining)])


def _render_tree_section(nodes: list[_AssumptionNode]) -> str:
    if not nodes:
        return ""
    lines = [
        "## Assumption Tree (from the earlier analysis levels)\n",
    ]
    for index, node in enumerate(nodes):
        marker = " (load-bearing)" if node.load_bearing else ""
        lines.append(f"{index}. {node.text}{marker}\n")
        for sub_index, sub in enumerate(node.sub_assumptions):
            lines.append(f"   {index}.{sub_index + 1} {sub}\n")
    lines.append("\n")
    return "".join(lines)


async def _call_assumptions_llm(
    state: WorkflowState,
    prompt: str,
    schema: Any,
    params: _AssumptionCallParams,
) -> dict[str, Any]:
    return await call_llm_json(
        prompt,
        spec=CompletionSpec(
            role="generation",
            model_name=state["model_name"],
            max_tokens=params.max_tokens,
            temperature=params.temperature,
            json_schema=schema,
        ),
        options=LLMCallOptions(
            run_id=state.get("run_id"),
            prompt_name=params.prompt_name,
        ),
    )


async def _build_assumption_tree(
    state: WorkflowState,
    reference_text: str,
    literature_context: str,
) -> tuple[list[_AssumptionNode], int]:
    """A failed tree must still reach final ideation; its template supports
    the no-tree case."""
    prompt, schema = load_prompt_with_schema(
        "generation_assumption_tree",
        {
            "research_goal": state["research_goal"],
            "max_top_assumptions": ASSUMPTION_TREE_MAX_TOP,
            "meta_review_context": _format_meta_review_context(state.get("meta_review")),
            "domain_context": "",
            "citation_reference_section": _build_citation_reference_section(reference_text),
            "literature_context": literature_context,
            "research_expansion_section": build_expansion_section(state),
            "falsified_assumptions_section": (
                build_falsified_assumptions_section(state.get("hypotheses"))
            ),
        },
    )
    response = await _call_assumptions_llm(state, prompt, schema, _TREE_LEVEL_PARAMS)
    nodes = _parse_top_assumptions(response)
    logger.info(
        "Assumption tree level 0: %s assumptions (%s load-bearing)",
        len(nodes),
        sum(1 for node in nodes if node.load_bearing),
    )

    parents = _select_parents(nodes)
    if not parents:
        return nodes, 1
    # Numbered parents join responses positionally without requiring the model to echo their text.
    parents_list = "".join(f"{index}. {parent.text}\n" for index, parent in enumerate(parents))
    sub_prompt, sub_schema = load_prompt_with_schema(
        "generation_assumption_sub",
        {
            "research_goal": state["research_goal"],
            "parents_list": parents_list,
            "max_sub_per_parent": ASSUMPTION_TREE_MAX_SUB_PER_PARENT,
            "domain_context": "",
            "citation_reference_section": _build_citation_reference_section(reference_text),
            "literature_context": literature_context,
        },
    )
    sub_response = await _call_assumptions_llm(state, sub_prompt, sub_schema, _SUB_LEVEL_PARAMS)
    _parse_sub_assumptions(sub_response, parents)
    logger.info(
        "Assumption tree level 1: %s parents, %s sub-assumptions",
        len(parents),
        sum(len(parent.sub_assumptions) for parent in parents),
    )
    return nodes, 2


async def generate_with_assumptions(
    state: WorkflowState,
    count: int,
    articles_with_reasoning: str | None = None,
    reference_index: ReferenceIndex | None = None,
) -> tuple[list[Hypothesis], int]:
    reference_text, sources, literature_context = _resolve_assumptions_context(
        reference_index, articles_with_reasoning
    )
    nodes, tree_calls = await _build_assumption_tree(state, reference_text, literature_context)
    tree_section = _render_tree_section(nodes)
    prompt, schema = load_prompt_with_schema(
        "generation_assumptions",
        {
            "research_goal": state["research_goal"],
            "num_hypotheses": count,
            "meta_review_context": _format_meta_review_context(state.get("meta_review")),
            "domain_context": "",
            "citation_reference_section": _build_citation_reference_section(reference_text),
            "lab_constraints_section": format_lab_constraints_section(state.get("lab_constraints")),
            "literature_context": literature_context,
            "assumption_tree_section": tree_section,
            "research_expansion_section": build_expansion_section(state),
            "falsified_assumptions_section": (
                build_falsified_assumptions_section(state.get("hypotheses"))
            ),
        },
    )
    response = await _call_assumptions_llm(state, prompt, schema, _FINAL_LEVEL_PARAMS)
    raw: list[dict[str, Any]] = response.get("hypotheses", [])
    hypotheses = [hypothesis_from_llm_output(h, sources, GenerationMethod.ASSUMPTIONS) for h in raw]
    logger.info(
        "Assumptions generation produced %s hypotheses from a %s-node tree",
        len(hypotheses),
        len(nodes),
    )
    return hypotheses, tree_calls + 1
