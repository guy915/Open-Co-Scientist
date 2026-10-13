"""Offline responses must stay deterministic and goal-grounded without
provider calls.
"""

import hashlib
import itertools
import json
import logging
import random
import re
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from co_scientist.platform.llm.request.backend import (
    CompletionBackend,
    active_backend,
    install_backend,
)

logger = logging.getLogger(__name__)


_STOPWORDS = frozenset(
    [
        "about",
        "above",
        "after",
        "against",
        "agent",
        "agents",
        "all",
        "also",
        "and",
        "are",
        "based",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "can",
        "could",
        "designed",
        "does",
        "drive",
        "driven",
        "during",
        "each",
        "either",
        "evolution",
        "from",
        "further",
        "generate",
        "generation",
        "goal",
        "had",
        "has",
        "have",
        "having",
        "help",
        "here",
        "how",
        "however",
        "hypotheses",
        "hypothesis",
        "identify",
        "into",
        "its",
        "itself",
        "meta-review",
        "might",
        "more",
        "most",
        "much",
        "must",
        "new",
        "novel",
        "only",
        "other",
        "over",
        "own",
        "particular",
        "potential",
        "propose",
        "proximity",
        "ranking",
        "reflection",
        "research",
        "review",
        "reviews",
        "same",
        "should",
        "since",
        "some",
        "such",
        "suggest",
        "supervisor",
        "task",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "under",
        "until",
        "use",
        "used",
        "using",
        "very",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "within",
        "would",
        "you",
        "your",
    ]
)

# Longest fragments win so constructive_feedback is feedback rather than a
# generic noun.
_TITLE_TEMPLATES = (
    "{term_a} modulation of {term_b}",
    "{term_a}-dependent control of {term_b}",
    "Rerouting {term_b} via {term_a}",
    "{term_a} as a rate-limiting constraint on {term_b}",
    "{term_b} under sustained {term_a} load",
    "Decoupling {term_a} from {term_b}",
)

_STATEMENT_TEMPLATES = (
    "Sustained modulation of {term_a} suppresses {term_b} in the "
    "conditions described, producing a measurable, dose-dependent shift "
    "relative to untreated controls",
    "{term_a} constrains {term_b} through a feedback loop that becomes "
    "rate-limiting once the upstream pool is depleted",
    "Perturbing {term_a} redirects flux away from {term_b}, and the "
    "effect persists after the initial stimulus is withdrawn",
    "{term_b} is set by the residence time of {term_a}, so slowing "
    "turnover raises the threshold at which the response appears",
    "Two routes converge on {term_b}; closing the {term_a} route "
    "unmasks the second rather than abolishing the output",
    "The dependence of {term_b} on {term_a} inverts above a threshold, "
    "which is why partial and full loss give opposite readouts",
)

_MECHANISM_TEMPLATES = (
    "{term_a} acts upstream of {term_b}, so the effect appears only "
    "once the intermediate pool is depleted",
    "Binding at {term_a} shifts the equilibrium toward {term_b} without changing total abundance",
    "The pathway routes through {term_a}; blocking it forces compensatory flux into {term_b}",
    "{term_a} occupies the site {term_b} needs, so the two compete rather than acting in series",
    "Turnover of {term_a} sets how long {term_b} stays available, "
    "making the timing of the perturbation decisive",
    "A slow conformational step gates {term_a}, and {term_b} reports "
    "that step rather than the binding event itself",
)

_EXPERIMENT_TEMPLATES = (
    "Titrate {term_a} across a five-point range and read out {term_b} at "
    "24 and 72 hours against a vehicle control",
    "Knock down {term_a} in matched lines and compare {term_b} with a "
    "rescue arm to confirm on-target effect",
    "Measure {term_b} under {term_a} perturbation, with a discriminating "
    "negative control that should show no shift",
    "Stage the {term_a} perturbation before and after the {term_b} "
    "window, since order separates cause from correlate",
    "Pair a chemical and a genetic route to {term_a}, and accept the "
    "{term_b} result only where the two agree",
    "Track {term_b} continuously through a single {term_a} pulse instead of sampling endpoints",
)

_CRITIQUE_TEMPLATES = (
    "The link to {term_a} is plausible but under-specified; the "
    "proposal would be stronger with a stated effect size",
    "Novelty is moderate -- {term_b} is well covered, though this "
    "framing of {term_a} is less explored",
    "The experiment tests {term_b} but not the {term_a} step it depends "
    "on, so a negative result would be hard to interpret",
    "Two mechanisms predict this {term_b} result equally well, and "
    "nothing here separates them from the {term_a} side",
    "The {term_a} claim rests on one readout; a second, orthogonal "
    "measure of {term_b} would carry most of the weight",
    "Scope is the weakness rather than logic -- the {term_a} argument "
    "holds only where {term_b} is already saturated",
)

_SUMMARY_TEMPLATES = (
    "Centres on {term_a} as the tractable handle on {term_b}",
    "A {term_a}-first account of {term_b}, testable in a single arm",
    "Argues {term_b} follows from {term_a} rather than the reverse",
    "Treats {term_b} as the readout and {term_a} as the lever",
    "Puts the decisive step between {term_a} and {term_b}",
    "Separates the {term_a} contribution to {term_b} from its context",
)

# Display-label fields need short phrases, never critique prose or appended
# scope clauses.
_GO_NO_GO_TEMPLATES = (
    "Go -- pursue the {term_a} experiment given the {term_b} evidence",
    "Go -- advance to validation, using {term_a} as the primary readout",
    "No-Go -- {term_a} remains unresolved without further {term_b} data",
    "Hold -- revisit once the {term_b} step clarifies {term_a}",
)

_TIME_ESTIMATE_TEMPLATES = (
    "Short (1-2 weeks)",
    "2-4 weeks",
    "1-2 months",
    "2-3 months",
    "One quarter",
)

_PHASE_LABEL_TEMPLATES = ("Phase A", "Phase B", "Phase C", "Stage 1", "Stage 2")

_RECOMMENDED_IDEA_TEMPLATES = (
    "Hypothesis 1",
    "Hypothesis 2",
    "Hypothesis 3",
    "Hypothesis 1, building on Hypothesis 2",
)

# Independent scope clauses diversify token bags; swapping the same terms does
# not avoid deduplication. Use study-design vocabulary so exclusions do not
# strip real biological goal terms.
_SCOPE_CLAUSES = (
    "in vehicle-matched replicates",
    "against a prespecified threshold",
    "once the washout period is complete",
    "at the low end of the titration",
    "before the compensatory arm engages",
    "with the readout blinded to condition",
    "across independently prepared batches",
    "holding the remaining variables fixed",
    "in the regime where the assay stays linear",
    "after the first exposure rather than the last",
    "where the baseline drift is smallest",
    "on the timescale the pathway actually turns over",
    "using a second, orthogonal readout",
    "with the confound removed by design",
    "restricted to the responders",
    "under the more conservative of two corrections",
    "and the direction survives the sensitivity analysis",
    "though the margin narrows in the replication arm",
    "with the effect concentrated in the earliest window",
    "which the pilot data already hint at",
    "though a ceiling appears at the top dose",
    "and nothing comparable is seen in the sham arm",
)

_FIELD_TEMPLATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Exact standalone field names precede broader fragment matches.
    ("go_no_go_recommendation", _GO_NO_GO_TEMPLATES),
    ("time_to_verdict", _TIME_ESTIMATE_TEMPLATES),
    ("time_estimate", _TIME_ESTIMATE_TEMPLATES),
    ("phase_label", _PHASE_LABEL_TEMPLATES),
    ("recommended_idea", _RECOMMENDED_IDEA_TEMPLATES),
    ("experiment", _EXPERIMENT_TEMPLATES),
    ("feedback", _CRITIQUE_TEMPLATES),
    ("critique", _CRITIQUE_TEMPLATES),
    ("reasoning", _CRITIQUE_TEMPLATES),
    ("rationale", _CRITIQUE_TEMPLATES),
    ("comparison", _CRITIQUE_TEMPLATES),
    ("weakness", _CRITIQUE_TEMPLATES),
    ("strength", _SUMMARY_TEMPLATES),
    ("mechanism", _MECHANISM_TEMPLATES),
    ("explanation", _MECHANISM_TEMPLATES),
    ("summary", _SUMMARY_TEMPLATES),
    ("overview", _SUMMARY_TEMPLATES),
    ("title", _TITLE_TEMPLATES),
    ("name", _TITLE_TEMPLATES),
    ("hypothesis", _STATEMENT_TEMPLATES),
    ("statement", _STATEMENT_TEMPLATES),
    ("text", _STATEMENT_TEMPLATES),
)

# Generic terms enter only for thin goals; mixing them into real subjects swamps
# the topic.
_FALLBACK_TERMS = (
    "upstream regulation",
    "rate-limiting control",
    "downstream signalling",
    "pathway flux",
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z-]{3,}")

# Exclude generated parent vocabulary to prevent offline evolution feeding on
# itself. Display-only label vocabulary stays available as real goal terms.
_GENERATED_VOCABULARY = frozenset(
    word.lower()
    for template in (
        *_TITLE_TEMPLATES,
        *_STATEMENT_TEMPLATES,
        *_MECHANISM_TEMPLATES,
        *_EXPERIMENT_TEMPLATES,
        *_CRITIQUE_TEMPLATES,
        *_SUMMARY_TEMPLATES,
        *_FALLBACK_TERMS,
        *_SCOPE_CLAUSES,
    )
    for word in _WORD_RE.findall(template)
)

# Anchor goal headings; evolution parent slots supply equivalent subject
# context.
_GOAL_HEADING_RE = re.compile(
    r"^[ \t]*(?:#+[ \t]*|\*\*)?"
    r"(?:research goal|original hypothesis|original conceptualization)"
    r"\b[:*]*[ \t]*",
    re.IGNORECASE | re.M,
)

# Bound the goal span before boilerplate can dominate extracted subject terms.
_GOAL_END_RE = re.compile(r"\n\s*\n|\n[ \t]*(?:#+|\*\*|-|\d+\.)")
_GOAL_SCAN_CHARS = 600


def _goal_text(prompt: str) -> str:
    """An unlabelled prompt must not mine role boilerplate as science; prefer
    generic clean fallback.
    """
    heading = _GOAL_HEADING_RE.search(prompt)
    if heading is None:
        return ""
    span = prompt[heading.end() : heading.end() + _GOAL_SCAN_CHARS]
    end = _GOAL_END_RE.search(span)
    return span[: end.start()] if end else span


def subject_terms(prompt: str) -> tuple[str, ...]:
    seen: list[str] = []
    for match in _WORD_RE.findall(_goal_text(prompt)):
        word = match.lower()
        if word in _STOPWORDS or word in _GENERATED_VOCABULARY:
            continue
        if word in seen:
            continue
        seen.append(word)
    return tuple(seen) if len(seen) >= 2 else _FALLBACK_TERMS


def _templates_for(field: str) -> tuple[str, ...]:
    lowered = field.lower()
    for fragment, templates in _FIELD_TEMPLATES:
        if fragment in lowered:
            return templates
    return _SUMMARY_TEMPLATES


# Standalone labels are complete phrases; appending scope prose creates a run-
# on.
_STANDALONE_TEMPLATES: tuple[tuple[str, ...], ...] = (
    _GO_NO_GO_TEMPLATES,
    _TIME_ESTIMATE_TEMPLATES,
    _PHASE_LABEL_TEMPLATES,
    _RECOMMENDED_IDEA_TEMPLATES,
)


def leaf_text(
    rng: random.Random,
    ordinal: int,
    field: str,
    terms: tuple[str, ...],
) -> str:
    """Per-leaf ordinals prevent identical sampled hypotheses being collapsed
    by deduplication.
    """
    templates = _templates_for(field)
    template = templates[rng.randrange(len(templates))]
    pool = terms if len(terms) >= 2 else terms + _FALLBACK_TERMS
    term_a, term_b = rng.sample(pool, 2)
    text = template.format(term_a=term_a, term_b=term_b)
    if templates is _TITLE_TEMPLATES:
        return f"{text[:1].upper()}{text[1:]} ({ordinal})"
    if templates in _STANDALONE_TEMPLATES:
        return f"{text[:1].upper()}{text[1:]}"
    # Draw scope clauses independently from subject terms to widen token-bag
    # diversity.
    clause = _SCOPE_CLAUSES[rng.randrange(len(_SCOPE_CLAUSES))]
    joiner = " " if clause.startswith("and ") else ", "
    return f"{text[:1].upper()}{text[1:]}{joiner}{clause}."


_SCALAR_DEFAULTS: dict[str, Any] = {
    "integer": 4,
    "number": 4.0,
    "boolean": True,
}


@dataclass(frozen=True)
class _FillHints:
    array_lengths: dict[str, int] = field(default_factory=dict)
    scalar_values: dict[str, Any] = field(default_factory=dict)
    optional_fields: frozenset[str] = frozenset()


def _scalar_leaf_value(schema_type: str, field: str, hints: _FillHints) -> Any:
    if field in hints.scalar_values:
        return hints.scalar_values[field]
    return _SCALAR_DEFAULTS[schema_type]


def _fill_schema(
    schema: dict[str, Any],
    leaf_fn: Callable[[str], Any],
    hints: _FillHints | None = None,
    field: str = "",
) -> Any:
    hints = hints or _FillHints()

    if "enum" in schema:
        return schema["enum"][0]

    schema_type = schema.get("type", "object")

    if schema_type == "object":
        return _fill_object(schema, leaf_fn, hints)

    if schema_type == "array":
        return _fill_array(schema, 1, leaf_fn, hints, field)

    if schema_type in _SCALAR_DEFAULTS:
        return _scalar_leaf_value(schema_type, field, hints)

    return leaf_fn(field)


def _fill_object(
    schema: dict[str, Any],
    leaf_fn: Callable[[str], Any],
    hints: _FillHints,
) -> dict[str, Any]:
    properties = schema.get("properties", {})
    required = schema.get("required") or list(properties.keys())
    return {
        name: _fill_property(name, prop_schema, leaf_fn, hints)
        for name, prop_schema in properties.items()
        if name in required or name in hints.optional_fields
    }


def _fill_property(
    name: str,
    schema: dict[str, Any],
    leaf_fn: Callable[[str], Any],
    hints: _FillHints,
) -> Any:
    if schema.get("type") == "array" and name in hints.array_lengths:
        return _fill_array(schema, hints.array_lengths[name], leaf_fn, hints, name)
    return _fill_schema(schema, leaf_fn, hints, name)


def _fill_array(
    schema: dict[str, Any],
    count: int,
    leaf_fn: Callable[[str], Any],
    hints: _FillHints,
    field: str = "",
) -> list[Any]:
    item_schema = schema.get("items", {"type": "string"})
    return [_fill_schema(item_schema, leaf_fn, hints, field) for _ in range(max(count, 1))]


OFFLINE_MODEL_PREFIX = "offline/"
DEFAULT_OFFLINE_MODEL = f"{OFFLINE_MODEL_PREFIX}deterministic"


def is_offline_model(model_name: str) -> bool:
    return model_name.startswith(OFFLINE_MODEL_PREFIX)


_HYPOTHESIS_MARKER_RE = re.compile(r"\*\*Hypothesis \d+:\*\*")


def _batch_review_length(prompt: str) -> dict[str, int]:
    count = len(_HYPOTHESIS_MARKER_RE.findall(prompt))
    return {"reviews": max(count, 1)}


_CANDIDATE_MARKER_RE = re.compile(r"\*\*Candidate \d+:\*\*")


def _relevance_batch_length(prompt: str) -> dict[str, int]:
    """Every candidate needs an offline judgment; the generic one-item filler
    would leave siblings unjudged.
    """
    count = len(_CANDIDATE_MARKER_RE.findall(prompt))
    return {"judgments": max(count, 1)}


# Overview directions need no input-count correspondence; request enough to pass
# the preview gate.
_RESEARCH_DIRECTIONS_COUNT = 3

# The exemplar's three unexpected directions exceed the filler's one-item
# default.
_UNEXPECTED_DIRECTIONS_COUNT = 3


def _research_overview_directions_length(_prompt: str) -> dict[str, int]:
    return {
        "research_directions": _RESEARCH_DIRECTIONS_COUNT,
        "unexpected_research_directions": _UNEXPECTED_DIRECTIONS_COUNT,
    }


_ARRAY_LENGTH_HINTS: dict[str, Callable[[str], dict[str, int]]] = {
    "hypothesis_batch_review": _batch_review_length,
    "research_overview": _research_overview_directions_length,
    "literature_relevance_batch": _relevance_batch_length,
}

# Offline scores must clear the initial gate to exercise deep reviews; pool
# indices keep generic defaults.
_REVIEW_SCORE_FIELDS: tuple[str, ...] = (
    "scientific_soundness",
    "plausibility",
    "novelty",
    "testability",
    "potential_impact",
    "relevance",
    "safety",
    "clarity",
    "overall_score",
)
_REVIEW_SCORE_VALUE = 7

_SCALAR_VALUE_HINTS: dict[str, dict[str, Any]] = {
    "hypothesis_review": dict.fromkeys(_REVIEW_SCORE_FIELDS, _REVIEW_SCORE_VALUE),
    "hypothesis_batch_review": dict.fromkeys(_REVIEW_SCORE_FIELDS, _REVIEW_SCORE_VALUE),
}

# Hint only named optional fields/schemas; filling every optional would
# misrepresent provider omissions.
_OPTIONAL_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "research_plan": ("question", "query"),
    "full_review": (
        # Required reviews_summary needs no optional hint; the schema itself
        # fills it.
        "go_no_go_recommendation",
        "time_to_verdict",
    ),
    "meta_review": (
        "time_estimate",
        "phase_label",
        "recommended_idea",
        "points",
    ),
}


def _build_response(content: str) -> Any:
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice])


def _prompt_text(completion_args: dict[str, Any]) -> str:
    messages = completion_args.get("messages") or []
    if not messages:
        return ""
    return str(messages[-1].get("content", ""))


def _seed_for(model: str, prompt: str, schema_name: str) -> int:
    digest = hashlib.sha256("\x00".join((model, prompt, schema_name)).encode("utf-8")).digest()
    return int.from_bytes(digest, byteorder="big")


def _supervisor_allocation_response(prompt: str) -> str:
    """Scheduling must react to prompt state rather than arbitrary schema-
    valid filler.
    """
    needs_proximity = '"pool_grew_since_proximity": true' in prompt
    first_cycle = '"iteration": 0' in prompt
    next_task = "proximity" if needs_proximity else ("evolve" if first_cycle else "generate")
    reason = (
        "Refresh the scientific similarity landscape."
        if needs_proximity
        else (
            "Improve reviewed leaders." if first_cycle else "Explore an underdeveloped direction."
        )
    )
    return json.dumps({"next_task": next_task, "reason": reason})


def _schema_response(model: str, prompt: str, json_schema: dict[str, Any]) -> Any:
    schema_name = json_schema.get("name", "")

    if schema_name == "supervisor_allocation":
        return _build_response(_supervisor_allocation_response(prompt))

    rng = random.Random(_seed_for(model, prompt, schema_name))
    ordinals = itertools.count(1)
    terms = subject_terms(prompt)

    def leaf_fn(field: str) -> str:
        return leaf_text(rng, next(ordinals), field, terms)

    schema = json_schema["schema"]
    length_hint = _ARRAY_LENGTH_HINTS.get(schema_name)
    hints = _FillHints(
        array_lengths=length_hint(prompt) if length_hint else {},
        scalar_values=_SCALAR_VALUE_HINTS.get(schema_name, {}),
        optional_fields=frozenset(_OPTIONAL_FIELD_HINTS.get(schema_name, ())),
    )
    content = json.dumps(_fill_schema(schema, leaf_fn, hints))
    return _build_response(content)


async def offline_acompletion(**completion_args: Any) -> Any:
    model = str(completion_args.get("model") or DEFAULT_OFFLINE_MODEL)
    prompt = _prompt_text(completion_args)
    response_format = completion_args.get("response_format")

    if response_format and response_format.get("type") == "json_schema":
        return _schema_response(model, prompt, response_format["json_schema"])

    if response_format and response_format.get("type") == "json_object":
        # Retain a safe schema-less JSON fallback even though current callers
        # supply schemas.
        return _build_response("{}")

    rng = random.Random(_seed_for(model, prompt, ""))
    return _build_response(leaf_text(rng, 1, "", subject_terms(prompt)))


_installed = False


class OfflineRouter:
    """Non-offline models retain the prior backend's completion and
    capability behavior.
    """

    def __init__(self, inner: CompletionBackend) -> None:
        self._inner = inner

    @property
    def operator_routing(self) -> bool:
        return bool(getattr(self._inner, "operator_routing", False))

    async def complete(self, **completion_args: Any) -> Any:
        model_name = str(completion_args.get("model") or "")
        if is_offline_model(model_name):
            return await offline_acompletion(**completion_args)
        return await self._inner.complete(**completion_args)

    def supports_json_schema(self, model_name: str) -> bool:
        if is_offline_model(model_name):
            return True
        return self._inner.supports_json_schema(model_name)


def install_offline_router() -> None:
    """Installation is idempotent; offline schemas use native enforcement
    rather than provider downgrade.
    """
    global _installed

    if _installed:
        return

    install_backend(OfflineRouter(active_backend()))
    _installed = True
    logger.debug("offline llm router installed")
