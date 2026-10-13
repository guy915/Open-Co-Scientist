from typing import Any

from co_scientist.science.schemas.finalist_review import FINALIST_REVIEW_SCHEMA
from co_scientist.science.schemas.generation import (
    ASSUMPTION_SUB_SCHEMA,
    ASSUMPTION_TREE_SCHEMA,
    GENERATION_DRAFT_SCHEMA,
    GENERATION_SCHEMA,
    HYPOTHESIS_NOVELTY_ANALYSIS_SCHEMA,
    HYPOTHESIS_VALIDATION_SYNTHESIS_SCHEMA,
)
from co_scientist.science.schemas.literature import (
    LITERATURE_PAPER_ANALYSIS_SCHEMA,
    LITERATURE_QUERY_SCHEMA,
    LITERATURE_RELEVANCE_BATCH_SCHEMA,
    RESEARCH_COMPRESS_SCHEMA,
    RESEARCH_EXTRACT_SCHEMA,
    RESEARCH_PLAN_SCHEMA,
    RESEARCH_QUERY_SCHEMA,
    RESEARCH_QUESTIONS_SCHEMA,
    RESEARCH_STANCES_SCHEMA,
)
from co_scientist.science.schemas.literature_batch import LITERATURE_PAPER_ANALYSIS_BATCH_SCHEMA
from co_scientist.science.schemas.planning import (
    META_REVIEW_SCHEMA,
    SUPERVISOR_SCHEMA,
)
from co_scientist.science.schemas.review import (
    DEEP_VERIFICATION_SCHEMA,
    FULL_REVIEW_SCHEMA,
    PROXIMITY_SCHEMA,
    RANKING_COMPARISON_CRITERIA,
    RANKING_SCHEMA,
    REFLECTION_SCHEMA,
    REVIEW_BATCH_SCHEMA,
    REVIEW_SCHEMA,
    SIMULATION_REVIEW_SCHEMA,
)
from co_scientist.science.schemas.synthesis import (
    EVOLUTION_SCHEMA,
    KNOWLEDGE_BASE_OUTLINE_SCHEMA,
    KNOWLEDGE_BASE_THEME_SCHEMA,
    RESEARCH_OVERVIEW_DIRECTION_SCHEMA,
    RESEARCH_OVERVIEW_INTERIM_SCHEMA,
    RESEARCH_OVERVIEW_REVIEW_SCHEMA,
    RESEARCH_OVERVIEW_SCHEMA,
)

# Keys match template filename stems; conversational prompts intentionally have
# no schema.
_PROMPT_SCHEMA_MAP: dict[str, dict[str, Any]] = {
    "research_plan": RESEARCH_PLAN_SCHEMA,
    "research_stances": RESEARCH_STANCES_SCHEMA,
    "research_questions": RESEARCH_QUESTIONS_SCHEMA,
    "research_query": RESEARCH_QUERY_SCHEMA,
    "research_extract": RESEARCH_EXTRACT_SCHEMA,
    "research_compress": RESEARCH_COMPRESS_SCHEMA,
    "generation_draft_with_tools": GENERATION_DRAFT_SCHEMA,
    "generation_assumptions": GENERATION_SCHEMA,
    "generation_assumption_tree": ASSUMPTION_TREE_SCHEMA,
    "generation_assumption_sub": ASSUMPTION_SUB_SCHEMA,
    "generation_debate_and_literature": GENERATION_SCHEMA,
    "generation_after_debate": GENERATION_SCHEMA,
    "review": REVIEW_SCHEMA,
    "review_batch": REVIEW_BATCH_SCHEMA,
    "full_review": FULL_REVIEW_SCHEMA,
    "simulation_review": SIMULATION_REVIEW_SCHEMA,
    "finalist_review": FINALIST_REVIEW_SCHEMA,
    "evolution": EVOLUTION_SCHEMA,
    "evolution_feasibility": EVOLUTION_SCHEMA,
    "evolution_out_of_box": EVOLUTION_SCHEMA,
    "meta_review": META_REVIEW_SCHEMA,
    "ranking_pairwise": RANKING_SCHEMA,
    "ranking_debate": RANKING_SCHEMA,
    "proximity": PROXIMITY_SCHEMA,
    "reflection_observations": REFLECTION_SCHEMA,
    "deep_verification": DEEP_VERIFICATION_SCHEMA,
    "research_overview": RESEARCH_OVERVIEW_SCHEMA,
    "research_overview_interim": RESEARCH_OVERVIEW_INTERIM_SCHEMA,
    "research_overview_knowledge_base_outline": KNOWLEDGE_BASE_OUTLINE_SCHEMA,
    "research_overview_knowledge_base_theme": KNOWLEDGE_BASE_THEME_SCHEMA,
    "research_overview_direction": RESEARCH_OVERVIEW_DIRECTION_SCHEMA,
    "research_overview_review": RESEARCH_OVERVIEW_REVIEW_SCHEMA,
    # Revision regenerates the whole overview and therefore shares the draft
    # schema.
    "research_overview_revise": RESEARCH_OVERVIEW_SCHEMA,
    "supervisor": SUPERVISOR_SCHEMA,
    "literature_review_paper_analysis": LITERATURE_PAPER_ANALYSIS_SCHEMA,
    "literature_review_paper_analysis_batch": LITERATURE_PAPER_ANALYSIS_BATCH_SCHEMA,
    "literature_review_relevance_batch": LITERATURE_RELEVANCE_BATCH_SCHEMA,
    "hypothesis_novelty_analysis": HYPOTHESIS_NOVELTY_ANALYSIS_SCHEMA,
    "hypothesis_validation_synthesis_with_tools": (HYPOTHESIS_VALIDATION_SYNTHESIS_SCHEMA),
}


def get_schema_for_prompt(prompt_name: str) -> dict[str, Any] | None:
    return _PROMPT_SCHEMA_MAP.get(prompt_name)


__all__ = [
    "ASSUMPTION_SUB_SCHEMA",
    "ASSUMPTION_TREE_SCHEMA",
    "DEEP_VERIFICATION_SCHEMA",
    "EVOLUTION_SCHEMA",
    "FULL_REVIEW_SCHEMA",
    "GENERATION_DRAFT_SCHEMA",
    "GENERATION_SCHEMA",
    "HYPOTHESIS_NOVELTY_ANALYSIS_SCHEMA",
    "HYPOTHESIS_VALIDATION_SYNTHESIS_SCHEMA",
    "LITERATURE_PAPER_ANALYSIS_SCHEMA",
    "LITERATURE_QUERY_SCHEMA",
    "LITERATURE_RELEVANCE_BATCH_SCHEMA",
    "META_REVIEW_SCHEMA",
    "PROXIMITY_SCHEMA",
    "RANKING_COMPARISON_CRITERIA",
    "RANKING_SCHEMA",
    "REFLECTION_SCHEMA",
    "RESEARCH_OVERVIEW_SCHEMA",
    "RESEARCH_PLAN_SCHEMA",
    "REVIEW_BATCH_SCHEMA",
    "REVIEW_SCHEMA",
    "SIMULATION_REVIEW_SCHEMA",
    "SUPERVISOR_SCHEMA",
    "get_schema_for_prompt",
]
