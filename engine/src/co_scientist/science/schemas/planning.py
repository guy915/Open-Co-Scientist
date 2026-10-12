from typing import Any, Final

from co_scientist.core.json_schema import obj, str_array

_MAX_CANDIDATE_COMPARISON_IDEAS: Final = 10
_MAX_EXISTING_SOLUTIONS_ROWS: Final = 6
_MAX_COMPARISON_AXES: Final = 5

# The published taxonomy nests themes, points and subpoints; bounds preserve
# that depth.
_MAX_RECURRING_THEMES: Final = 6
_MAX_SUB_THEMES: Final = 8
_MAX_SUB_THEME_POINTS: Final = 5

META_REVIEW_SCHEMA: dict[str, Any] = {
    "name": "meta_review",
    "strict": False,
    "schema": obj(
        {
            "meta_review_summary": {
                "type": "string",
                "description": "Overall summary of meta-review analysis",
            },
            "recurring_themes": {
                "type": "array",
                "maxItems": _MAX_RECURRING_THEMES,
                "description": (
                    "A taxonomy of the recurring critiques, not a flat"
                    " list: a handful of broad themes, each holding the"
                    " named critique points that recur under it, each of"
                    " those holding the concrete guidance a future"
                    " proposal should follow."
                ),
                "items": obj(
                    {
                        "theme": {
                            "type": "string",
                            "description": (
                                "A broad critique theme spanning several"
                                " reviews, e.g. 'Core Hypothesis and"
                                " Mechanism' or 'Experimental Design and"
                                " Feasibility'."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "One or two sentences on what this theme"
                                " covers across the reviewed pool."
                            ),
                        },
                        "frequency": {
                            "type": "string",
                            "description": (
                                "How often this theme recurs, in the"
                                " reviews' own terms, e.g. 'very common'"
                                " or 'raised on most hypotheses'."
                            ),
                        },
                        "sub_themes": {
                            "type": "array",
                            "maxItems": _MAX_SUB_THEMES,
                            "description": (
                                "The named critique points recurring"
                                " under this theme. Empty only when the"
                                " theme genuinely has none."
                            ),
                            "items": obj(
                                {
                                    "theme": {
                                        "type": "string",
                                        "description": (
                                            "The critique point's own"
                                            " short name, e.g. 'Primary"
                                            " Driver vs. Consequence' or"
                                            " 'Model System"
                                            " Limitations'."
                                        ),
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": (
                                            "What reviewers said, and how"
                                            " widely -- state the"
                                            " prevalence in this prose"
                                            " ('a very common critique',"
                                            " 'several ideas') rather"
                                            " than as a count."
                                        ),
                                    },
                                    "points": {
                                        "type": "array",
                                        "maxItems": _MAX_SUB_THEME_POINTS,
                                        "items": {"type": "string"},
                                        "description": (
                                            "Concrete guidance a future"
                                            " proposal should follow to"
                                            " answer this critique. One"
                                            " short sentence each. Empty"
                                            " where the point needs no"
                                            " breakdown."
                                        ),
                                    },
                                },
                                optional=("points",),
                            ),
                        },
                    }
                ),
            },
            "strengths": str_array(),
            "weaknesses": str_array(),
            "process_assessment": obj(
                {
                    "generation_process": {"type": "string"},
                    "review_process": {"type": "string"},
                    "evolution_process": {"type": "string"},
                }
            ),
            "strategic_recommendations": {
                "type": "array",
                "items": obj(
                    {
                        "focus_area": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "justification": {"type": "string"},
                        # Optional richer roadmap fields accommodate exemplars
                        # that carry time, subphases or ideas.
                        "time_estimate": {
                            "type": "string",
                            "description": (
                                "This step's timeline, in the run's own"
                                " units, e.g. 'Weeks 1-2' or 'Month 3+'."
                                " Empty when this step carries no"
                                " explicit timeline."
                            ),
                        },
                        "phase_label": {
                            "type": "string",
                            "description": (
                                "A lettered sub-phase name when this"
                                " step splits into parts, e.g."
                                " 'Phase A'. Empty otherwise."
                            ),
                        },
                        "recommended_idea": {
                            "type": "string",
                            "description": (
                                "Names which reviewed idea(s) this step"
                                " selects, by the same hypothesis_index"
                                " convention as candidate_comparison."
                                "ideas (e.g. 'Hypothesis 1, building on"
                                " Hypothesis 4'). Refer to ideas by"
                                " number only -- never restate their"
                                " text. Empty when this step does not"
                                " single out one idea."
                            ),
                        },
                    },
                    optional=(
                        "time_estimate",
                        "phase_label",
                        "recommended_idea",
                    ),
                ),
            },
            "potential_connections": {
                "type": "array",
                "items": obj(
                    {
                        "related_hypotheses": str_array(),
                        "connection_type": {"type": "string"},
                        "synthesis_opportunity": {"type": "string"},
                    }
                ),
            },
            "candidate_comparison": obj(
                {
                    "thematic_summary": {
                        "type": "string",
                        "description": (
                            "How the candidate hypotheses group into"
                            " mechanistic themes, and which is best"
                            " supported by the reviewed evidence."
                        ),
                    },
                    "axes": {
                        "type": "array",
                        "maxItems": _MAX_COMPARISON_AXES,
                        "items": {"type": "string"},
                        "description": (
                            "2-5 short axis names to compare the ideas"
                            " on, chosen to fit THIS run's own"
                            " discipline -- e.g. 'Off-target risk' for a"
                            " chemical-biology goal, 'Cohort"
                            " availability' for a clinical-epidemiology"
                            " one. Do not default to generic"
                            " engineering/computational vocabulary"
                            " ('scalability', 'implementation"
                            " complexity') for a wet-lab biology"
                            " question it does not fit."
                        ),
                    },
                    "ideas": {
                        "type": "array",
                        "maxItems": _MAX_CANDIDATE_COMPARISON_IDEAS,
                        "items": obj(
                            {
                                "idea": {
                                    "type": "string",
                                    "description": (
                                        "The hypothesis_index and its"
                                        " subject, e.g. 'Hypothesis 3:"
                                        " LILRB4 blockade' -- never the"
                                        " full hypothesis text."
                                    ),
                                },
                                "values": str_array(
                                    "One rating per entry in `axes`, in"
                                    " the same order -- values[i]"
                                    " answers axes[i] for this idea."
                                ),
                            }
                        ),
                    },
                }
            ),
            "existing_solutions_comparison": obj(
                {
                    "summary": {
                        "type": "string",
                        "description": (
                            "How current standard-of-care approaches"
                            " compare to the candidate hypotheses as a"
                            " group. Leave this and `rows` empty when"
                            " this goal has no established"
                            " standard-of-care or existing-solutions"
                            " landscape to compare against (e.g. a basic"
                            " mechanism question) rather than inventing"
                            " one."
                        ),
                    },
                    "axes": {
                        "type": "array",
                        "maxItems": _MAX_COMPARISON_AXES,
                        "items": {"type": "string"},
                        "description": (
                            "2-5 short axis names to compare each"
                            " existing approach against the candidate"
                            " ideas on, chosen to fit this run's own"
                            " discipline -- see candidate_comparison"
                            "'s axes for the same guidance."
                        ),
                    },
                    "rows": {
                        "type": "array",
                        "maxItems": _MAX_EXISTING_SOLUTIONS_ROWS,
                        "items": obj(
                            {
                                "method": {
                                    "type": "string",
                                    "description": (
                                        "Name of the existing approach"
                                        " or standard-of-care baseline"
                                        " being compared."
                                    ),
                                },
                                "values": str_array(
                                    "One rating per entry in `axes`, in"
                                    " the same order -- values[i]"
                                    " answers axes[i] for this method."
                                ),
                            }
                        ),
                    },
                }
            ),
            # Narrative main directions are distinct from the itemized research-
            # direction array.
            "main_research_directions": {
                "type": "string",
                "description": (
                    "A narrative synthesis of this run's main research"
                    " directions, distinct from any itemized"
                    " per-direction list. Write exactly two flowing prose"
                    " paragraphs, separated by a blank line -- never a"
                    " bullet list. Name each direction inline in **bold**"
                    " the first time it appears (e.g. '**Metabolic State"
                    " as an Intervention Point**'), explain briefly why"
                    " it matters, and close the second paragraph with an"
                    " unanticipated observation that cuts across more"
                    " than one direction (e.g. 'Unexpectedly, recent"
                    " synthesis suggests ...'). Mirrors the published"
                    ' report\'s own "Main Research Directions" section:'
                    " connected narrative prose, not a restatement of"
                    " strategic_recommendations above."
                ),
            },
        }
    ),
}


_AGENTS_ASSESSED: tuple[str, ...] = (
    "generation",
    "reflection",
    "ranking",
    "evolution",
    "proximity",
    "meta_review",
)

# Six criteria accommodate the full rubric; questions are bounded separately.
CRITICAL_CRITERIA_MAX_COUNT: Final = 6
CRITICAL_CRITERIA_MAX_QUESTIONS: Final = 4

SUPERVISOR_SCHEMA: dict[str, Any] = {
    "name": "supervisor_guidance",
    "strict": False,
    "schema": obj(
        {
            "research_goal_analysis": obj(
                {
                    "goal_summary": {
                        "type": "string",
                        "description": ("concise restatement of the research goal"),
                    },
                    "key_areas": str_array(),
                    "constraints_identified": str_array(),
                    "success_criteria": str_array(),
                }
            ),
            "workflow_plan": obj(
                {
                    "generation_phase": obj(
                        {
                            "focus_areas": str_array(),
                            "diversity_targets": {
                                "type": "string",
                                "description": ("description of diversity targets for hypotheses"),
                            },
                            "quantity_target": {
                                "type": "string",
                                "description": "target number of hypotheses",
                            },
                        }
                    ),
                    "review_phase": obj(
                        {
                            # Criterion prose is report-only; questions supply
                            # the per-hypothesis operational guidance.
                            "critical_criteria": {
                                "type": "array",
                                "maxItems": CRITICAL_CRITERIA_MAX_COUNT,
                                "description": (
                                    "Up to 6 domain-specific criteria"
                                    " reviewers should emphasize, each with"
                                    " a prose description of what it"
                                    " demands and up to 4 named yes/no"
                                    " questions a reviewer would ask when"
                                    " checking a hypothesis against it."
                                ),
                                "items": obj(
                                    {
                                        "name": {
                                            "type": "string",
                                            "description": ("short criterion name"),
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": (
                                                "one prose paragraph"
                                                " stating what this"
                                                " criterion demands of a"
                                                " hypothesis and why it"
                                                " matters for this"
                                                " research goal"
                                            ),
                                        },
                                        "questions": {
                                            "type": "array",
                                            "maxItems": (CRITICAL_CRITERIA_MAX_QUESTIONS),
                                            "items": obj(
                                                {
                                                    "name": {
                                                        "type": "string",
                                                        "description": (
                                                            "short name for this question"
                                                        ),
                                                    },
                                                    "question": {
                                                        "type": "string",
                                                        "description": (
                                                            "a specific"
                                                            " yes/no"
                                                            " question a"
                                                            " reviewer"
                                                            " would ask"
                                                        ),
                                                    },
                                                }
                                            ),
                                        },
                                    }
                                ),
                            },
                            "review_depth": {
                                "type": "string",
                                "description": "depth of review required",
                            },
                        }
                    ),
                    "evolution_phase": obj(
                        {
                            "refinement_priorities": str_array(),
                            "iteration_strategy": {
                                "type": "string",
                                "description": ("description of iteration strategy"),
                            },
                        }
                    ),
                }
            ),
            "config_synthesis": {
                **obj(
                    {
                        "preferences": str_array(
                            "Hard scope constraints plus the soft 'what makes"
                            " a good idea' qualities. Used by BOTH generation"
                            " and review."
                        ),
                        "draft_instructions": str_array(
                            "Writing guidance for the drafting generator"
                            " ONLY, which writes hypotheses straight from the"
                            " literature. Do NOT restate the preferences."
                        ),
                        "debate_instructions": str_array(
                            "Writing guidance for the debate generator ONLY,"
                            " which argues a hypothesis out over several"
                            " turns. Do NOT restate the preferences."
                        ),
                        "review_instructions": str_array(
                            "Comparative critique guidance for reviewers"
                            " ONLY: how to validate soundness and tell strong"
                            " ideas from weak ones. Do NOT restate the"
                            " preferences here."
                        ),
                        "attributes": {
                            "type": "array",
                            "description": (
                                "Up to 3 axes used to stratify and compare"
                                " ideas, each with a 1-5 scoring rubric."
                            ),
                            "items": obj(
                                {
                                    "name": {
                                        "type": "string",
                                        "description": "short attribute name",
                                    },
                                    "rubric": {
                                        "type": "string",
                                        "description": (
                                            "how to score this attribute from 1 (worst) to 5 (best)"
                                        ),
                                    },
                                }
                            ),
                        },
                    }
                ),
                "description": (
                    "Normalized run configuration synthesized from the goal,"
                    " mirroring the reference product's Config. Keep the four"
                    " instruction lists strictly separate."
                ),
            },
            # Observability fields persist in state without becoming downstream
            # prompt instructions.
            "performance_assessment": obj(
                {
                    "current_status": {
                        "type": "string",
                        "description": "assessment of current workflow status",
                    },
                    "bottlenecks_identified": str_array(),
                    "agent_performance": obj(
                        {
                            f"{agent}_agent": {
                                "type": "string",
                                "description": (
                                    f"assessment of {agent.replace('_', '-')} agent performance"
                                ),
                            }
                            for agent in _AGENTS_ASSESSED
                        }
                    ),
                }
            ),
            "adjustment_recommendations": {
                "type": "array",
                "items": obj(
                    {
                        "aspect": {
                            "type": "string",
                            "description": "aspect to adjust",
                        },
                        "adjustment": {
                            "type": "string",
                            "description": "description of adjustment",
                        },
                        "justification": {
                            "type": "string",
                            "description": "reasoning behind this adjustment",
                        },
                    }
                ),
            },
            "output_preparation": obj(
                {
                    "hypothesis_selection_strategy": {
                        "type": "string",
                        "description": ("strategy for selecting final hypotheses"),
                    },
                    "presentation_format": {
                        "type": "string",
                        "description": ("format for presenting results to scientist"),
                    },
                    "key_insights_to_highlight": str_array(),
                }
            ),
        }
    ),
}
