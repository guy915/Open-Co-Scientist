from typing import Any, Final

from co_scientist.core.json_schema import obj, str_array

# Parent identity uses prompt indices, bounding output without echoing long
# hypotheses.
ASSUMPTION_TREE_SCHEMA: dict[str, Any] = {
    "name": "assumption_tree",
    "strict": False,
    "schema": obj(
        {
            "assumptions": {
                "type": "array",
                "description": ("The area's key taken-for-granted assumptions"),
                "items": obj(
                    {
                        "assumption": {
                            "type": "string",
                            "description": (
                                "One assumption currently taken for granted in this research area"
                            ),
                        },
                        "load_bearing": {
                            "type": "boolean",
                            "description": (
                                "True if this assumption is load-bearing:"
                                " much of the area's reasoning depends on"
                                " it, so challenging it would open new"
                                " hypothesis space"
                            ),
                        },
                    }
                ),
            }
        }
    ),
}
ASSUMPTION_SUB_SCHEMA: dict[str, Any] = {
    "name": "assumption_sub_assumptions",
    "strict": False,
    "schema": obj(
        {
            "parents": {
                "type": "array",
                "description": (
                    "Sub-assumption decompositions, one entry per expanded parent assumption"
                ),
                "items": obj(
                    {
                        "parent_index": {
                            "type": "integer",
                            "description": (
                                "The 0-based index of the parent"
                                " assumption in the numbered list the"
                                " prompt supplied"
                            ),
                        },
                        "sub_assumptions": str_array(
                            "The finer-grained sub-assumptions the parent decomposes into"
                        ),
                    }
                ),
            }
        }
    ),
}
HYPOTHESIS_NOVELTY_ANALYSIS_SCHEMA: dict[str, Any] = {
    "name": "hypothesis_novelty_analysis",
    "strict": False,
    "schema": obj(
        {
            "methods_used": {
                "type": "string",
                "description": "what methods/techniques this paper employs",
            },
            "populations_studied": {
                "type": "string",
                "description": "what populations/contexts are covered",
            },
            "mechanisms_investigated": {
                "type": "string",
                "description": "what mechanisms/targets are studied",
            },
            "key_findings": {
                "type": "string",
                "description": "main findings relevant to the hypothesis",
            },
            "stated_limitations": {
                "type": "string",
                "description": "limitations or gaps the authors mention",
            },
            "future_work_suggested": {
                "type": "string",
                "description": "future directions the authors propose",
            },
            "novelty_assessment": {
                "type": "string",
                "description": "how hypothesis compares to this paper",
                "enum": [
                    "overlapping",
                    "complementary",
                    "orthogonal",
                    "addresses_gaps",
                ],
            },
            "overlap_explanation": {
                "type": "string",
                "description": ("detailed explanation of how hypothesis compares to this paper"),
            },
        }
    ),
}

# Shared schema objects must remain immutable across callers.
_HYPOTHESIS_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "Mechanistic scientific hypothesis stated in"
        " the natural language of the goal's"
        " domain: name the entities, mechanism,"
        " direction of effect, conditions, and"
        " the specific testable prediction. Do"
        " not use a fixed 'We want to develop'"
        " phrasing or an artificial length cap"
    ),
}

# Downgraded JSON responses need defensive title validation at persistence.
MAX_TITLE_CHARS: Final = 120

_TITLE_FIELD: dict[str, Any] = {
    "type": "string",
    "maxLength": MAX_TITLE_CHARS,
    "description": (
        "A short, authored name for this hypothesis: a compact noun"
        f" phrase under {MAX_TITLE_CHARS} characters, e.g. 'Rapamycin"
        " Suppression of mTOR-Driven Growth Signaling'. Never a full"
        " sentence, never a restatement or truncation of the hypothesis"
        " text itself, and no trailing period."
    ),
}

_EXPLANATION_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "Step-by-step layman explanation breaking"
        " down the technical hypothesis: trace"
        " each mechanistic step from intervention"
        " to outcome and say why each step is"
        " expected to hold. Depth over brevity;"
        " a full paragraph, not a summary"
    ),
}

# Proposer pilot thresholds are design details, never reviewer gates.
MAX_EXPERIMENT_STEPS: Final = 5
_EXPERIMENT_STEP_CHARS: Final = 300
_EXPERIMENT_CRITERION_CHARS: Final = 300

_EXPERIMENT_STEP_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "One ordered step of the concrete pilot plan: what is done and"
        " what it establishes. 1-2 sentences."
    ),
}

_EXPERIMENT_FIELD: dict[str, Any] = obj(
    {
        "steps": {
            "type": "array",
            "items": _EXPERIMENT_STEP_FIELD,
            # Offline schema filling emits one item; rendering accepts fewer
            # than the advisory count. json_object mode needs a defensive
            # maximum.
            "maxItems": MAX_EXPERIMENT_STEPS,
            "description": (
                f"2-{MAX_EXPERIMENT_STEPS} ordered steps of the pilot"
                " test plan -- typically scripting/automation setup,"
                " then ground-truth calibration, then an"
                " outgroup/control comparison, ending with the Go/No-Go"
                " initial experiment step itself. State the pass/fail"
                " threshold in go_criterion/no_go_criterion below, not"
                " here."
            ),
        },
        "go_criterion": {
            "type": "string",
            "description": (
                "The exact quantitative pass threshold for the Go/No-Go"
                " initial experiment step: the specific result that"
                " would justify continuing to the next phase."
            ),
        },
        "no_go_criterion": {
            "type": "string",
            "description": (
                "The exact quantitative fail threshold for the Go/No-Go"
                " initial experiment step: the specific result that"
                " would justify abandoning or substantially revising"
                " the approach."
            ),
        },
    }
)

# Draft experiment prose is discarded; only synthesis supplies the structured
# experiment.
_EXPERIMENT_DRAFT_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "Complete experiment design: model system,"
        " groups and controls, quantitative"
        " readouts with expected effect sizes or"
        " thresholds, and validation criteria"
        " distinguishing support from falsification."
        " Depth over brevity; a full paragraph,"
        " not a sketch"
    ),
}

_LITERATURE_GROUNDING_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "2-4 sentences grounding the hypothesis in"
        " the provided reference list. Use ONLY the"
        " bracketed [C*] citation keys supplied"
        " (e.g. [C1], [C2], [C3]) — do NOT invent"
        " author-year citations. If no reference"
        " list was provided, state that explicitly."
    ),
}

# Scene-setting repeats per hypothesis; bound it independently of pool size.
_INTRODUCTION_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "2-4 sentences of scene-setting background: the problem area"
        " this hypothesis addresses and why it matters, before any"
        " specific mechanism is proposed. Matches the published"
        " 'Introduction' section."
    ),
}

_RECENT_FINDINGS_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "2-4 sentences summarizing recent findings and related"
        " research this hypothesis builds on, extends, or departs"
        " from. Matches the published 'Recent findings and related"
        " research' section. Distinct from literature_grounding: this"
        " sets the scene, literature_grounding argues the specific"
        " hypothesis."
    ),
}

# Proposer-authored safety assessments must never affect their safety gate.
_SAFETY_TOXICITY_FIELD: dict[str, Any] = {
    "type": "string",
    "description": (
        "2-4 sentences on the safety profile of what this hypothesis"
        " proposes: for a pharmacological intervention, known or"
        " expected toxicity and what preclinical safety work would be"
        " needed before advancing it; for other domains, the analogous"
        " operational or experimental safety considerations. This is"
        " your own assessment as the proposer, not a review."
    ),
}

GENERATION_SCHEMA: dict[str, Any] = {
    "name": "hypothesis_generation",
    "strict": False,
    "schema": obj(
        {
            "hypotheses": {
                "type": "array",
                "items": obj(
                    {
                        "title": _TITLE_FIELD,
                        "introduction": _INTRODUCTION_FIELD,
                        "recent_findings": _RECENT_FINDINGS_FIELD,
                        "hypothesis": _HYPOTHESIS_FIELD,
                        "explanation": _EXPLANATION_FIELD,
                        "literature_grounding": _LITERATURE_GROUNDING_FIELD,
                        "experiment": _EXPERIMENT_FIELD,
                        # The prompt requests categorization; required fields
                        # keep the output contract aligned.
                        "category": {
                            "type": "string",
                            "description": (
                                "Short (2-4 word) classification label naming"
                                " the mechanism family or research sub-area"
                                " this hypothesis belongs to, e.g."
                                " 'Metabolic reprogramming' or 'Epitope"
                                " editing'. Used to group and label ideas."
                                " Hypotheses from the same mechanism family"
                                " must carry the same label."
                            ),
                        },
                        "safety_and_toxicity": _SAFETY_TOXICITY_FIELD,
                    },
                ),
            }
        }
    ),
}
GENERATION_DRAFT_SCHEMA: dict[str, Any] = {
    "name": "hypothesis_draft",
    "strict": False,
    "schema": obj(
        {
            "drafts": {
                "type": "array",
                "items": obj(
                    {
                        "hypothesis": _HYPOTHESIS_FIELD,
                        "explanation": _EXPLANATION_FIELD,
                        "experiment": _EXPERIMENT_DRAFT_FIELD,
                        "gap_reasoning": {
                            "type": "string",
                            "description": (
                                "Brief explanation of what gap in the"
                                " literature this hypothesis addresses and"
                                " why it seems promising"
                            ),
                        },
                        "literature_sources": {
                            "type": "string",
                            "description": (
                                "Sources from the reference list that"
                                " informed this gap. Use ONLY the bracketed"
                                " [C*] keys provided (e.g. [C1], [C2],"
                                " [C3]). Example: 'Gap identified via"
                                " retinal imaging findings [C1] and tau"
                                " isoform research [C2][C3].'"
                            ),
                        },
                    },
                ),
            }
        }
    ),
}
HYPOTHESIS_VALIDATION_SYNTHESIS_SCHEMA: dict[str, Any] = {
    "name": "hypothesis_validation_synthesis",
    "strict": False,
    "schema": obj(
        {
            "hypotheses": {
                "type": "array",
                "items": obj(
                    {
                        "title": _TITLE_FIELD,
                        "introduction": _INTRODUCTION_FIELD,
                        "recent_findings": _RECENT_FINDINGS_FIELD,
                        "hypothesis": {
                            "type": "string",
                            "description": (
                                "Final mechanistic scientific hypothesis"
                                " text in the goal's domain language, naming"
                                " entities, mechanism, direction of effect,"
                                " and the testable prediction, without a fixed"
                                " phrasing or length cap (approved/refined/"
                                "pivoted)"
                            ),
                        },
                        "explanation": _EXPLANATION_FIELD,
                        "literature_grounding": _LITERATURE_GROUNDING_FIELD,
                        "experiment": _EXPERIMENT_FIELD,
                        # Validation retains the generation prompt's required
                        # categorization contract.
                        "category": {
                            "type": "string",
                            "description": (
                                "Short (2-4 word) classification label naming"
                                " the mechanism family or research sub-area"
                                " this hypothesis belongs to. Hypotheses from"
                                " the same mechanism family must carry the"
                                " same label."
                            ),
                        },
                        "novelty_validation": obj(
                            {
                                "decision": {
                                    "type": "string",
                                    "description": "validation decision",
                                    "enum": ["approved", "refined", "pivoted"],
                                }
                            }
                        ),
                        "safety_and_toxicity": _SAFETY_TOXICITY_FIELD,
                    },
                ),
            }
        }
    ),
}


# Evolution fields describe the child, never a copied parent experiment or
# title.

EVOLUTION_SCHEMA: dict[str, Any] = {
    "name": "hypothesis_evolution",
    "strict": False,
    "schema": obj(
        {
            "title": _TITLE_FIELD,
            "introduction": _INTRODUCTION_FIELD,
            "recent_findings": _RECENT_FINDINGS_FIELD,
            "literature_grounding": _LITERATURE_GROUNDING_FIELD,
            "safety_and_toxicity": _SAFETY_TOXICITY_FIELD,
            "hypothesis": {
                "type": "string",
                "description": (
                    "Refined mechanistic hypothesis in the domain's"
                    " natural language, naming entities, mechanism, and"
                    " the testable prediction (no fixed phrasing)."
                ),
            },
            "refinement_summary": {
                "type": "string",
                "description": ("Summary of changes and improvements made during evolution."),
            },
            "explanation": {
                "type": "string",
                "description": (
                    "Updated step-by-step layman explanation reflecting"
                    " any refinements made (4-6 sentences)"
                ),
            },
            "experiment": _EXPERIMENT_FIELD,
            "combined_partners": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "Combination operator only: the 1-based positional "
                    "indices of the partner hypotheses whose mechanisms "
                    "this refinement merges. Omit for every other operator "
                    "and never repeat a partner's text."
                ),
            },
        },
        # Combination partner indices identify parents without echoing text;
        # other operators omit them.
        optional=("combined_partners",),
    ),
}
