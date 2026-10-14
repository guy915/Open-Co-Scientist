from co_scientist.platform.retrieval.evidence.relevance import register_judgment_prompt
from co_scientist.science.prompts._common import (
    PromptRunContext,
    format_lab_constraints_section,
)
from co_scientist.science.prompts.generation_debate import (
    DebatePromptRequest,
    get_debate_generation_prompt,
)
from co_scientist.science.prompts.generation_draft import (
    DraftPromptRequest,
    build_tool_instructions,
    format_articles_metadata,
    format_attributes,
    format_preferences,
    format_supervisor_guidance_for_generation,
    format_user_hypotheses,
    get_draft_prompt_with_tools,
)
from co_scientist.science.prompts.literature import (
    LiteratureQueryInputs,
    ValidationSynthesisRequest,
    get_hypothesis_novelty_analysis_prompt,
    get_hypothesis_query_generation_prompt,
    get_literature_review_paper_analysis_prompt,
    get_literature_review_query_generation_prompt,
    get_literature_review_relevance_batch_prompt,
    get_literature_review_synthesis_prompt,
    get_validation_synthesis_prompt_with_tools,
)
from co_scientist.science.prompts.loading import (
    load_prompt,
    load_prompt_with_schema,
    substitute_variables,
)
from co_scientist.science.prompts.planning import (
    DirectionWritingMaterial,
    OverviewReviewMaterial,
    OverviewRevisionRequest,
    SupervisorPromptInputs,
    ThemeWritingMaterial,
    get_knowledge_base_outline_prompt,
    get_knowledge_base_theme_prompt,
    get_meta_review_prompt,
    get_research_overview_direction_prompt,
    get_research_overview_interim_prompt,
    get_research_overview_prompt,
    get_research_overview_review_prompt,
    get_research_overview_revise_prompt,
    get_supervisor_prompt,
)
from co_scientist.science.prompts.ranking import (
    RankingSide,
    get_proximity_prompt,
    get_ranking_prompt,
)
from co_scientist.science.prompts.review import (
    get_deep_verification_prompt,
    get_reflection_prompt,
    get_review_batch_prompt,
    get_review_prompt,
)
from co_scientist.science.schemas import LITERATURE_RELEVANCE_BATCH_SCHEMA

__all__ = [
    "DebatePromptRequest",
    "DirectionWritingMaterial",
    "DraftPromptRequest",
    "LiteratureQueryInputs",
    "OverviewReviewMaterial",
    "OverviewRevisionRequest",
    "PromptRunContext",
    "RankingSide",
    "SupervisorPromptInputs",
    "ThemeWritingMaterial",
    "ValidationSynthesisRequest",
    "build_tool_instructions",
    "format_articles_metadata",
    "format_attributes",
    "format_lab_constraints_section",
    "format_preferences",
    "format_supervisor_guidance_for_generation",
    "format_user_hypotheses",
    "get_debate_generation_prompt",
    "get_deep_verification_prompt",
    "get_draft_prompt_with_tools",
    "get_hypothesis_novelty_analysis_prompt",
    "get_hypothesis_query_generation_prompt",
    "get_knowledge_base_outline_prompt",
    "get_knowledge_base_theme_prompt",
    "get_literature_review_paper_analysis_prompt",
    "get_literature_review_query_generation_prompt",
    "get_literature_review_relevance_batch_prompt",
    "get_literature_review_synthesis_prompt",
    "get_meta_review_prompt",
    "get_proximity_prompt",
    "get_ranking_prompt",
    "get_reflection_prompt",
    "get_research_overview_direction_prompt",
    "get_research_overview_interim_prompt",
    "get_research_overview_prompt",
    "get_research_overview_review_prompt",
    "get_research_overview_revise_prompt",
    "get_review_batch_prompt",
    "get_review_prompt",
    "get_supervisor_prompt",
    "get_validation_synthesis_prompt_with_tools",
    "load_prompt",
    "load_prompt_with_schema",
    "substitute_variables",
]

register_judgment_prompt(
    get_literature_review_relevance_batch_prompt, LITERATURE_RELEVANCE_BATCH_SCHEMA
)
