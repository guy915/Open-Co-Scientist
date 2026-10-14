from typing import Any

from co_scientist.core.json_schema import obj, str_array

# These fixed aspects are shared by schema keys, prompts and persisted ranking
# reviews.
RANKING_COMPARISON_CRITERIA: tuple[str, ...] = (
    "correctness_comparison",
    "utility_comparison",
    "detail_comparison",
    "novelty_comparison",
    "desirability_comparison",
)

# decision_summary supplies the primary winner; better_idea remains the
# fallback.
RANKING_SCHEMA: dict[str, Any] = {
    "name": "ranking_judgment",
    "strict": False,
    "schema": obj(
        {
            "winner": {
                "type": "string",
                "enum": ["a", "b"],
                "description": "The winning hypothesis (a or b)",
            },
            "judgment_explanation": obj(
                {name: {"type": "string"} for name in RANKING_COMPARISON_CRITERIA}
            ),
            "decision_summary": {"type": "string"},
            "confidence_level": {
                "type": "string",
                "enum": ["High", "Medium", "Low"],
            },
        }
    ),
}
PROXIMITY_SCHEMA: dict[str, Any] = {
    "name": "proximity_analysis",
    "strict": False,
    "schema": obj(
        {
            "similarity_clusters": {
                "type": "array",
                "items": obj(
                    {
                        "cluster_id": {"type": "string"},
                        "cluster_name": {"type": "string"},
                        "central_theme": {"type": "string"},
                        # Positional indices identify members without response
                        # growth from echoing hypotheses.
                        "similar_hypotheses": {
                            "type": "array",
                            "items": obj(
                                {
                                    "index": {"type": "integer"},
                                    "similarity_degree": {
                                        "type": "string",
                                        "enum": ["high", "medium", "low"],
                                    },
                                }
                            ),
                        },
                        "synthesis_potential": {"type": "string"},
                    }
                ),
            },
            "diversity_assessment": {"type": "string"},
            "redundancy_assessment": {"type": "string"},
        }
    ),
}


# Deep and full reviews share support values; ranking filters match them.
ASSUMPTION_SUPPORT_VALUES: tuple[str, ...] = (
    "supported",
    "uncertain",
    "likely_false",
)

# Full executive summaries enrich drained reviews; the initial review_summary
# remains a string.
REVIEWS_SUMMARY_MAX_ITEMS = 5

# Published summaries use opening/closing prose with bulleted middle parts.
_REVIEWS_SUMMARY_LISTS: dict[str, str] = {
    "critical_flaws": (
        "Each entry one flaw that would sink the hypothesis if left"
        " unaddressed, naming the specific claim it breaks."
    ),
    "addressed_objections": (
        "Each entry one objection raised during review that the"
        " hypothesis or its evidence answers, and how."
    ),
    "validated_risks": (
        "Each entry one risk or limitation that survives review: real,"
        " but not on its own disqualifying."
    ),
    "supporting_arguments": (
        "Each entry one argument or piece of evidence that motivates the hypothesis."
    ),
    "alignment_and_novelty": (
        "Each entry one statement on how the hypothesis aligns with the"
        " research goal, or on what is genuinely new in it."
    ),
    "feasibility_assessment": (
        "Each entry one feasibility consideration -- resource"
        " intensity, technical complexity, or time to a decisive"
        " result."
    ),
}

REVIEWS_SUMMARY_SCHEMA: dict[str, Any] = obj(
    {
        "executive_verdict": {
            "type": "string",
            "description": (
                "A paragraph stating what the hypothesis proposes and"
                " whether it stands, ending in an explicit verdict."
            ),
        },
        **{
            name: {
                **str_array(description),
                "maxItems": REVIEWS_SUMMARY_MAX_ITEMS,
            }
            for name, description in _REVIEWS_SUMMARY_LISTS.items()
        },
        "conclusion": {
            "type": "string",
            "description": (
                "A closing paragraph: what the hypothesis is worth, and"
                " what would have to change for it to be worth testing."
            ),
        },
    }
)


# Rich axis structures are full-review only; batch size multiplies their output
# cost.
FEASIBILITY_STEPS_MAX_ITEMS = 5

_PER_AXIS_REVIEW_SCHEMA: dict[str, Any] = {
    "comparison_with_knowledge_base": {
        "type": "string",
        "description": (
            "How the hypothesis sits against established knowledge in"
            " the field: what it agrees with, what it contradicts."
        ),
    },
    "goal_requirements_assessment": {
        "type": "string",
        "description": (
            "Whether the hypothesis meets each requirement the research"
            " goal states, naming any requirement it does not meet."
        ),
    },
    "feasibility_steps": {
        **str_array(
            "The concrete steps that would test this hypothesis, each"
            " entry one step, in the order they would be run."
        ),
        "maxItems": FEASIBILITY_STEPS_MAX_ITEMS,
    },
    "feasibility_reasoning": {
        "type": "string",
        "description": (
            "Why those steps are or are not practical: the resources,"
            " techniques and time a decisive result would take."
        ),
    },
    "impact_assessment": {
        "type": "string",
        "description": (
            "What changes in the field if the hypothesis holds, and how"
            " much: the overall impact potential."
        ),
    },
}


# Related abstracts are attached from evidence rather than echoed by the model.
FULL_REVIEW_SCHEMA: dict[str, Any] = {
    "name": "full_review",
    "schema": obj(
        {
            "correctness": {"type": "string"},
            # The full executive summary is separate from the initial gate's
            # review_summary string.
            "reviews_summary": REVIEWS_SUMMARY_SCHEMA,
            "assumptions": {
                "type": "array",
                "items": obj(
                    {
                        "assumption": {"type": "string"},
                        "reasoning": {
                            "type": "string",
                            "description": (
                                "2-4 sentences of free-text reasoning for"
                                " why the evidence does or does not back"
                                " this assumption, referencing specific"
                                " evidence where available."
                            ),
                        },
                        "support": {
                            "type": "string",
                            "enum": list(ASSUMPTION_SUPPORT_VALUES),
                        },
                    }
                ),
            },
            "quality_and_novelty": {"type": "string"},
            "literature_grounding": {"type": "string"},
            # Full-review axis names remain persistence and renderer keys.
            **_PER_AXIS_REVIEW_SCHEMA,
            "verdict": {
                "type": "string",
                "enum": ["sound", "needs_revision", "rejected"],
            },
            "justification": {"type": "string"},
            # Go/No-Go annotations are display-only; the overall verdict
            # controls tournament eligibility.
            "go_no_go_recommendation": {
                "type": "string",
                "description": (
                    "A short free-text testing recommendation, e.g. 'Go —"
                    " pursue wet-lab validation' or 'No-Go — mechanism"
                    " unsupported'. Advisory framing for the reader; it"
                    " does not replace verdict above."
                ),
            },
            "time_to_verdict": {
                "type": "string",
                "description": (
                    "A brief estimated timeframe to reach a decisive"
                    " experimental result, e.g. 'Short', '2-4 weeks', or"
                    " '2-3 months'."
                ),
            },
        },
        optional=(
            # Only 8 of 19 published full reviews include Go/No-Go; missing
            # annotations are legitimate.
            "go_no_go_recommendation",
            "time_to_verdict",
        ),
    ),
}


# Required fields match the prompt to avoid paying for retries on correct
# responses.

# Axis ordering controls model output order; downstream readers use names.
_SCORE_CRITERIA: tuple[str, ...] = (
    "scientific_soundness",
    "plausibility",
    "novelty",
    "testability",
    "potential_impact",
    "relevance",
    "safety",
    "clarity",
)

# The shared 1-10 rubric preserves calibrated gate thresholds.
REVIEW_SCORE_MINIMUM: int = 1
REVIEW_SCORE_MAXIMUM: int = 10

_SCORES_SCHEMA: dict[str, Any] = obj(
    {
        name: {
            "type": "integer",
            "minimum": REVIEW_SCORE_MINIMUM,
            "maximum": REVIEW_SCORE_MAXIMUM,
        }
        for name in _SCORE_CRITERIA
    }
)

# Axis output ordering matches the full review while retaining name-based
# consumers.
_FEEDBACK_DESCRIPTIONS: dict[str, str] = {
    "scientific_soundness": ("Specific feedback on theoretical foundation and logical consistency"),
    "novelty": ("Specific feedback on originality and unique contribution"),
    "testability": "Specific feedback on feasibility of testing",
    "potential_impact": "Specific feedback on potential significance",
    "relevance": "Specific feedback on alignment with research goal",
    "clarity": ("Specific feedback on precision and clarity of formulation"),
}

_DETAILED_FEEDBACK_SCHEMA: dict[str, Any] = obj(
    {
        name: {"type": "string", "description": description}
        for name, description in _FEEDBACK_DESCRIPTIONS.items()
    }
)

NOVELTY_REVIEW_MAX_ITEMS = 6

# Explored and novel aspects describe distinct contributions, separately from
# novelty scores.
_NOVELTY_REVIEW_SCHEMA: dict[str, Any] = obj(
    {
        "already_explored": {
            **str_array(
                "Aspects of the hypothesis already covered by existing"
                " work known to you, each entry one sentence naming what"
                " is already explored. Leave empty if nothing overlaps."
            ),
            "maxItems": NOVELTY_REVIEW_MAX_ITEMS,
        },
        "novel_aspects": {
            **str_array(
                "Aspects of the hypothesis you have not seen explored"
                " before, each entry one sentence naming what is novel."
                " Leave empty if nothing is novel."
            ),
            "maxItems": NOVELTY_REVIEW_MAX_ITEMS,
        },
    }
)

_REVIEW_BODY_PROPS: dict[str, Any] = {
    "review_summary": {
        "type": "string",
        "description": "Overall assessment (2-3 sentences)",
    },
    "scores": _SCORES_SCHEMA,
    "detailed_feedback": _DETAILED_FEEDBACK_SCHEMA,
    "constructive_feedback": {
        "type": "string",
        "description": "Specific, actionable suggestions for improvement",
    },
    "safety_ethical_concerns": {
        "type": "string",
        "description": "Any ethical or safety concerns",
    },
    "novelty_review": _NOVELTY_REVIEW_SCHEMA,
}

REVIEW_SCHEMA: dict[str, Any] = {
    "name": "hypothesis_review",
    "strict": False,
    "schema": obj(
        {
            "hypothesis_text": {
                "type": "string",
                "description": "The hypothesis being reviewed",
            },
            **_REVIEW_BODY_PROPS,
            "overall_score": {
                "type": "number",
                "description": "Calculated as average of criterion scores",
            },
        }
    ),
}
# Batch members use prompt indices rather than echoing their hypotheses.
REVIEW_BATCH_SCHEMA: dict[str, Any] = {
    "name": "hypothesis_batch_review",
    "strict": False,
    "schema": obj(
        {
            "reviews": {
                "type": "array",
                "description": "Array of reviews, one for each hypothesis",
                "items": obj(
                    {
                        "hypothesis_index": {
                            "type": "integer",
                            "description": (
                                "The number assigned to the hypothesis in"
                                " the prompt: 1 for Hypothesis 1, 2 for"
                                " Hypothesis 2, and so on"
                            ),
                        },
                        **_REVIEW_BODY_PROPS,
                        "comparative_notes": {
                            "type": "string",
                            "description": (
                                "Brief note on how this hypothesis compares to the others"
                            ),
                        },
                    },
                    optional=("comparative_notes",),
                ),
            }
        }
    ),
}
# Bound model-authored strengths independently of hypothesis count.
REFLECTION_MAX_POSITIVE_OBSERVATIONS = 5

REFLECTION_SCHEMA: dict[str, Any] = {
    "name": "reflection_observations",
    "strict": False,
    "schema": obj(
        {
            "hypothesis_text": {
                "type": "string",
                "description": "The hypothesis being analyzed",
            },
            "reasoning": {
                "type": "string",
                "description": "Detailed reasoning for the classification",
            },
            "classification": {
                "type": "string",
                "enum": [
                    "already explained",
                    "other explanations more likely",
                    "missing piece",
                    "neutral",
                    "disproved",
                ],
                "description": ("Classification of hypothesis based on literature observations"),
            },
            "positive_observations": {
                **str_array(
                    "Observations the hypothesis genuinely explains well:"
                    " confirmed strengths where it provides a superior or"
                    " mechanistically distinct explanation. Leave empty"
                    " when the review found none."
                ),
                "maxItems": REFLECTION_MAX_POSITIVE_OBSERVATIONS,
            },
        },
        optional=("positive_observations",),
    ),
}

# Bound both model-authored decomposition lists independently of the corpus.
DEEP_VERIFICATION_MAX_SUB_ASSUMPTIONS = 5
DEEP_VERIFICATION_MAX_DECONTEXTUALIZATIONS = 3

DEEP_VERIFICATION_SCHEMA: dict[str, Any] = {
    "name": "deep_verification",
    "schema": obj(
        {
            "probes": {
                "type": "array",
                "items": obj(
                    {
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                        "reasoning": {"type": "string"},
                        "assumption_is_fundamental": {"type": "boolean"},
                        # Literature retrieval needs AND-able search terms;
                        # prose questions retrieve poorly.
                        "search_query": {"type": "string"},
                    }
                ),
            },
            "sub_assumptions": {
                "type": "array",
                "maxItems": DEEP_VERIFICATION_MAX_SUB_ASSUMPTIONS,
                "items": obj(
                    {
                        "assumption": {"type": "string"},
                        "verification": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": list(ASSUMPTION_SUPPORT_VALUES),
                        },
                    }
                ),
            },
            "decontextualizations": {
                "type": "array",
                "maxItems": DEEP_VERIFICATION_MAX_DECONTEXTUALIZATIONS,
                "items": obj(
                    {
                        "context_bound_claim": {"type": "string"},
                        "general_claim": {"type": "string"},
                        "assessment": {"type": "string"},
                    }
                ),
            },
            "verdict": {
                "type": "string",
                "enum": ["holds", "weakened", "undermined"],
            },
            "overall_assessment": {"type": "string"},
        }
    ),
}


# Simulation fields match the prompt under a closed schema to avoid undeclared-
# field retries.
SIMULATION_REVIEW_SCHEMA: dict[str, Any] = {
    "name": "simulation_review",
    "schema": obj(
        {
            "model": {"type": "string"},
            "steps": {
                "type": "array",
                "items": obj(
                    {
                        "step": {"type": "string"},
                        "plausible": {"type": "boolean"},
                    }
                ),
            },
            "failure_points": str_array(),
            "robustness": {"type": "string"},
            "verdict": {
                "type": "string",
                "enum": ["holds", "partially_holds", "breaks_down"],
            },
            "decisive_step": {"type": "string"},
        }
    ),
}
