from typing import Any, Final

from co_scientist.core.json_schema import obj, str_array
from co_scientist.science.schemas.generation import (
    EVOLUTION_SCHEMA as EVOLUTION_SCHEMA,
)

# Nested arrays multiply output; the exemplar uses four subtopics with four
# questions. json_object mode also needs defensive bounds.
RESEARCH_OVERVIEW_MAX_SUB_TOPICS: Final = 4
RESEARCH_OVERVIEW_MAX_SUB_TOPIC_QUESTIONS: Final = 4

# Six directions are developed in separate calls to stay within the provider
# deadline.
RESEARCH_OVERVIEW_MAX_DIRECTIONS: Final = 6

# Published open questions number ten; pattern lists are shorter and
# independently bounded.
RESEARCH_OVERVIEW_MAX_OPEN_QUESTIONS: Final = 10
RESEARCH_OVERVIEW_MAX_PATTERNS: Final = 5

# Terminal synthesis already reaches the token ceiling; new fields must not
# scale with the pool.
RESEARCH_OVERVIEW_MAX_UNEXPECTED_DIRECTIONS: Final = 3

# The published knowledge base has 43 subjects under eight themes; ceilings
# leave room above its target.
KNOWLEDGE_BASE_MAX_THEMES: Final = 8
KNOWLEDGE_BASE_MAX_SECTIONS: Final = 8

# Dots inside tokens preserve decimals and variant notation; spell out dotted
# abbreviations so a sentence boundary cannot hide a second sentence.
_KNOWLEDGE_BASE_SUMMARY: Final[dict[str, Any]] = {
    "type": "string",
    "pattern": r"^(?=\S+(?:[ \t]+\S+){0,29}$)(?:[^.!?\r\n]|\.(?=\S))+[.!?]?$",
    "description": (
        "Exactly one concise sentence, at most 30 words, grounded in the detail"
        " and preserving its main limitation where relevant. Spell out dotted"
        " abbreviations; do not put sentence-ending punctuation before the final word."
    ),
}

_RESEARCH_DIRECTION_BODY: Final[dict[str, Any]] = {
    "importance": {"type": "string"},
    "suggested_experiments": str_array(),
    # Published exemplars differ: recent findings may be separate or folded into
    # the rationale.
    "recent_findings": {"type": "string"},
    # Subtopics are authored content, not input hypotheses echoed by index.
    "sub_topics": {
        "type": "array",
        "maxItems": (RESEARCH_OVERVIEW_MAX_SUB_TOPICS),
        "items": obj(
            {
                "title": {"type": "string"},
                "why": {"type": "string"},
                "what": {"type": "string"},
                # The exemplar distinguishes a concrete attack from the topic
                # and its questions.
                "example_idea": {
                    "type": "string",
                    "description": (
                        "One concrete worked"
                        " example of how this"
                        " sub-topic would"
                        " actually be"
                        " investigated:"
                        " the approach, what"
                        " it measures, and"
                        " what the result"
                        " would show."
                    ),
                },
                "specific_questions": {
                    **str_array(),
                    "maxItems": (RESEARCH_OVERVIEW_MAX_SUB_TOPIC_QUESTIONS),
                },
            }
        ),
    },
}
# Draft and wave direction bodies share one shape that must not drift.

RESEARCH_OVERVIEW_DIRECTION_SCHEMA: dict[str, Any] = {
    "name": "research_overview_direction",
    "schema": obj(dict(_RESEARCH_DIRECTION_BODY)),
}
# Omit titles: draft titles are contact/group cross-reference identities.

RESEARCH_OVERVIEW_SCHEMA: dict[str, Any] = {
    "name": "research_overview",
    "schema": obj(
        {
            "overview": obj(
                {
                    "summary": {"type": "string"},
                    "research_directions": {
                        "type": "array",
                        # The exemplar names six directions; json_object mode
                        # also needs a defensive cap.
                        "maxItems": RESEARCH_OVERVIEW_MAX_DIRECTIONS,
                        "items": obj(
                            {
                                "title": {"type": "string"},
                                **_RESEARCH_DIRECTION_BODY,
                            }
                        ),
                    },
                }
            ),
            # Published Specific Aims pages use Disease Description, Unmet Need,
            # Proposed Solution, aims and Pilot Evaluation.
            "nih_specific_aims": obj(
                {
                    "disease_description": {"type": "string"},
                    "unmet_need": {"type": "string"},
                    "proposed_solution": {"type": "string"},
                    "aims": {
                        "type": "array",
                        "items": obj(
                            {
                                "overarching_goal": {"type": "string"},
                                "hypothesis": {"type": "string"},
                                "reasoning": {"type": "string"},
                            }
                        ),
                    },
                    "pilot_evaluation": {"type": "string"},
                }
            ),
            "research_contacts": {
                "type": "array",
                "maxItems": 5,
                "items": obj(
                    {
                        "candidate_id": {"type": "string"},
                        "name": {"type": "string"},
                        "expertise": {"type": "string"},
                        "justification": {"type": "string"},
                        # Direction labels are cross-references, unlike factual
                        # names or affiliations.
                        "research_direction": {"type": "string"},
                    }
                ),
            },
            # Groups add shared rationale and two examples while preserving
            # legacy flat contact rendering.
            "research_contact_groups": {
                "type": "array",
                "maxItems": 5,
                "items": obj(
                    {
                        "research_direction": {
                            "type": "string",
                            "description": (
                                "Must exactly match one of the"
                                " research_direction values used in"
                                " research_contacts above -- this is"
                                " how a group's contacts are found."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "One shared paragraph explaining why"
                                " this group of contacts, together, is"
                                " well suited to lead work on this"
                                " research direction -- distinct from"
                                " each contact's own individual"
                                " justification."
                            ),
                        },
                        "example_hypothesis_indices": {
                            "type": "array",
                            "maxItems": 2,
                            "items": {"type": "integer"},
                            "description": (
                                "Up to two 1-based positions from the"
                                " numbered hypothesis list above whose"
                                " ideas exemplify this research"
                                " direction. Refer to hypotheses by"
                                " number only -- never invent or echo a"
                                " title."
                            ),
                        },
                    },
                    optional=("example_hypothesis_indices",),
                ),
            },
            "knowledge_base": {
                "type": "array",
                "maxItems": 8,
                "items": obj(
                    {
                        "title": {"type": "string"},
                        "summary": dict(_KNOWLEDGE_BASE_SUMMARY),
                        "detail": {"type": "string"},
                        "uncertainty": {"type": "string"},
                        "evidence_ids": str_array(),
                    }
                ),
            },
            # Open questions and observed patterns belong beside the research-
            # direction synthesis.
            "open_questions": {
                **str_array(
                    "The most important unanswered questions this"
                    " synthesis leaves open, each a specific testable"
                    " question -- not a restatement of the research"
                    " goal."
                ),
                "maxItems": RESEARCH_OVERVIEW_MAX_OPEN_QUESTIONS,
            },
            "clear_patterns": {
                **str_array(
                    "Patterns that recur clearly across the synthesized hypotheses and evidence."
                ),
                "maxItems": RESEARCH_OVERVIEW_MAX_PATTERNS,
            },
            "unexpected_patterns": {
                **str_array(
                    "Patterns or connections that were not anticipated"
                    " going in, surfaced only by synthesizing across the"
                    " hypotheses and evidence together."
                ),
                "maxItems": RESEARCH_OVERVIEW_MAX_PATTERNS,
            },
            # Novel strategic directions are distinct from expected directions
            # and observed patterns.
            "unexpected_research_directions": {
                "type": "array",
                "maxItems": RESEARCH_OVERVIEW_MAX_UNEXPECTED_DIRECTIONS,
                "description": (
                    "Up to 3 genuinely novel research directions worth"
                    " pursuing that were not anticipated going in and are"
                    " not among the main research_directions above --"
                    " surfaced only by synthesizing across the hypotheses"
                    " and evidence together. Return an empty list rather"
                    " than restating a main direction or stretching an"
                    " expected one to sound unexpected."
                ),
                "items": obj(
                    {
                        "title": {
                            "type": "string",
                            "description": ("short name for the novel direction"),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "one prose paragraph explaining the"
                                " direction and why it is worth pursuing"
                            ),
                        },
                    }
                ),
            },
        }
    ),
}

# Interim synthesis asks only for titles/questions the next cycle reads, with
# matching render caps.
RESEARCH_OVERVIEW_INTERIM_MAX_DIRECTIONS: Final = 4
RESEARCH_OVERVIEW_INTERIM_MAX_QUESTIONS: Final = 5

RESEARCH_OVERVIEW_INTERIM_SCHEMA: dict[str, Any] = {
    "name": "research_overview_interim",
    "schema": obj(
        {
            "overview": obj(
                {
                    "research_directions": {
                        "type": "array",
                        "maxItems": RESEARCH_OVERVIEW_INTERIM_MAX_DIRECTIONS,
                        "items": obj({"title": {"type": "string"}}),
                    },
                }
            ),
            "open_questions": {
                **str_array(
                    "The most important unanswered questions this run's"
                    " progress so far leaves open, each a specific"
                    " testable question -- not a restatement of the"
                    " research goal."
                ),
                "maxItems": RESEARCH_OVERVIEW_INTERIM_MAX_QUESTIONS,
            },
        }
    ),
}
# Keep nesting identical so interim parsing accepts periodic and terminal
# responses.

# Located accuracy notes avoid echoing the draft and scaling output with input.
RESEARCH_OVERVIEW_REVIEW_SCHEMA: dict[str, Any] = {
    "name": "research_overview_review",
    "schema": obj(
        {
            "accept": {
                "type": "boolean",
                "description": (
                    "True only when every claim in the drafted overview "
                    "is supported by the listed hypotheses and evidence, "
                    "with no contradiction and no overstated confidence. "
                    "False for a genuine accuracy problem -- never for "
                    "tone, style, or length."
                ),
            },
            "notes": {
                "type": "array",
                "maxItems": 6,
                "items": obj(
                    {
                        "location": {
                            "type": "string",
                            "description": (
                                "The section or claim this note concerns "
                                "(e.g. 'overview.summary', 'aims[2]', or "
                                "a knowledge-base topic's title) -- never "
                                "the passage text itself."
                            ),
                        },
                        "issue": {
                            "type": "string",
                            "description": (
                                "The specific accuracy problem: an "
                                "unsupported claim, a contradiction with "
                                "a listed hypothesis, overstated "
                                "confidence the material does not carry, "
                                "or a cited evidence_id that does not "
                                "support what it is cited for."
                            ),
                        },
                        "evidence_id": {
                            "type": "string",
                            "description": (
                                "The evidence_id involved, when the issue concerns a citation."
                            ),
                        },
                    },
                    optional=("evidence_id",),
                ),
            },
        },
        optional=("notes",),
    ),
}


# Prompt and schema word bands must agree, including under json_object
# downgrade.
KNOWLEDGE_BASE_SECTION_WORDS: Final = (200, 300)

KNOWLEDGE_BASE_PRINCIPAL_SECTION_WORDS: Final = (350, 500)
# A distinct principal-subject band avoids flattening every section to the
# same length.

_ORDINARY: Final = "{}-{}".format(*KNOWLEDGE_BASE_SECTION_WORDS)
_PRINCIPAL: Final = "{}-{}".format(*KNOWLEDGE_BASE_PRINCIPAL_SECTION_WORDS)

_EVIDENCE_IDS = (
    "The evidence_ids this subsection is synthesized from. Listed ids"
    " only; a subsection citing none is dropped."
)

_HEADING = "Name of this subsection's specific subject."

_THEME_TITLE = (
    "Name of the subject area, as a heading (e.g. 'Extracellular Matrix"
    " Architecture And Biomechanical Barriers')."
)

# One outline prevents independent writers overlapping or renumbering; source
# IDs avoid corpus echo.
KNOWLEDGE_BASE_OUTLINE_SCHEMA: dict[str, Any] = {
    "name": "knowledge_base_outline",
    "schema": obj(
        {
            "themes": {
                "type": "array",
                "maxItems": KNOWLEDGE_BASE_MAX_THEMES,
                "description": (
                    "The subject areas this run's evidence covers, in"
                    " reading order, each holding its own named"
                    " subsections."
                ),
                "items": obj(
                    {
                        "title": {
                            "type": "string",
                            "description": _THEME_TITLE,
                        },
                        "sections": {
                            "type": "array",
                            "maxItems": KNOWLEDGE_BASE_MAX_SECTIONS,
                            "items": obj(
                                {
                                    "heading": {
                                        "type": "string",
                                        "description": _HEADING,
                                    },
                                    "evidence_ids": str_array(_EVIDENCE_IDS),
                                }
                            ),
                        },
                    }
                ),
            }
        }
    ),
}

# One theme per call fits the provider deadline; prose has no maxLength to avoid
# visible trimming.
KNOWLEDGE_BASE_THEME_SCHEMA: dict[str, Any] = {
    "name": "knowledge_base_theme",
    "schema": obj(
        {
            "sections": {
                "type": "array",
                "maxItems": KNOWLEDGE_BASE_MAX_SECTIONS,
                "description": ("This theme's subsections, in the order the outline lists them."),
                "items": obj(
                    {
                        "heading": {
                            "type": "string",
                            "description": _HEADING,
                        },
                        "detail": {
                            "type": "string",
                            "description": (
                                "Dense encyclopedic prose:"
                                f" {_ORDINARY} words, or"
                                f" {_PRINCIPAL} for a theme's two or"
                                " three principal subjects."
                                " Name every entity the evidence gives"
                                " for this subject and every parameter,"
                                " effect size, threshold and unit it"
                                " states. No citation markers and no"
                                " bullet lists."
                            ),
                        },
                        "summary": dict(_KNOWLEDGE_BASE_SUMMARY),
                        "evidence_ids": str_array(_EVIDENCE_IDS),
                    }
                ),
            }
        }
    ),
}
