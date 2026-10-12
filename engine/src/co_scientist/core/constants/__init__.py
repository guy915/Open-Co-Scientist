import hashlib
from collections.abc import Iterable
from typing import Final

from co_scientist.core.citations import strip_citation_markers as strip_citation_markers

DEFAULT_MAX_TOKENS: Final = 4000
MAX_PLANNING_IDEAS: Final = 96
MAX_PLANNING_ITERATIONS: Final = 8
MAX_PLANNING_K_FACTOR: Final = 512

EXTENDED_MAX_TOKENS: Final = 8000

LONG_MAX_TOKENS: Final = 10000

DEEP_HYPOTHESIS_MAX_TOKENS: Final = 12000
# Mechanism, predictions and experiment detail require a larger answer budget
# than generic calls.

RESEARCH_OVERVIEW_MAX_TOKENS: Final = 24000
# Terminal strategy and Specific Aims share total allowance with reasoning; a
# floor-sized budget can truncate them.

KNOWLEDGE_BASE_OUTLINE_MAX_TOKENS: Final = 6000
# Outline once and write bounded themes: a 42k stream exceeds the measured
# provider clock.

KNOWLEDGE_BASE_THEME_MAX_TOKENS: Final = 12000
# Eight 500-word sections need about 8k prose tokens plus JSON; bounded parts
# fit the provider deadline.

RESEARCH_OVERVIEW_DIRECTION_MAX_TOKENS: Final = 6000
# Separate direction calls bound latency and lose only one direction on
# failure, never all six.

RESEARCH_OVERVIEW_INTERIM_MAX_TOKENS: Final = 6000
# Periodic output feeds only titles/questions; the answer shrinks but thinking
# still receives its floor.

THINKING_MAX_TOKENS: Final = 18000
# The thinking floor derives from this proven allowance; changing it affects
# all reasoning calls.

BUDGET_ESCALATION_MAX_TOKENS: Final = 24000
# Retries must increase even an already-floor-sized request, then disable
# reasoning if needed.

BUDGET_ESCALATION_MAX_INCREMENT: Final = BUDGET_ESCALATION_MAX_TOKENS
# Bound the increment, not the result, so already-large requests still receive
# a real escalation.

THINKING_FLOOR_MAX_TOKENS: Final = THINKING_MAX_TOKENS
# Provider max_tokens includes reasoning and answer; apply the floor only
# where the model actually reasons.

MINIMAL_REASONING_MAX_TOKENS: Final = 2048
# Bound mandatory reasoning itself; larger output budgets fund more thought.
# Exceed Anthropic's minimum while leaving room for classification answers.


def scaled_max_tokens(
    base: int, count: int, *, per_item: int, cap: int, free_count: int = 0
) -> int:
    """Only model-aware dispatch can apply the thinking floor; scaling must
    not fund non-thinking providers.
    """
    return min(base + max(0, count - free_count) * per_item, cap)


# Keep caps above the thinking floor so both provider regimes share a ceiling.
REVIEW_BATCH_TOKENS_PER_HYPOTHESIS: Final = 1500

REVIEW_BATCH_FREE_HYPOTHESES: Final = 5

REVIEW_BATCH_MAX_TOKENS_CAP: Final = 24000

EVOLVE_TOKENS_PER_CONTEXT_HYPOTHESIS: Final = 800

EVOLVE_MAX_TOKENS_CAP: Final = 20000

DEBATE_FINAL_TURN_TOKENS_PER_HYPOTHESIS: Final = 4000

DEBATE_FINAL_TURN_MAX_TOKENS_CAP: Final = 20000

DRAFT_TOKENS_PER_HYPOTHESIS: Final = 200

DRAFT_MAX_TOKENS_CAP: Final = 20000
# Keep count-dependent caps above the reasoning floor so they constrain both
# provider regimes.

VALIDATION_SYNTHESIS_TOKENS_PER_HYPOTHESIS: Final = 2500

VALIDATION_SYNTHESIS_MAX_TOKENS_CAP: Final = 20000


INITIAL_ELO_RATING: Final = 1200

# The paper does not specify a K-factor or annealing/margin schedule.
ELO_K_FACTOR: Final = 24


ELO_K_ANNEALING_HALF_LIFE: Final = 0
# Annealing is an optional local reconstruction; zero preserves the fixed
# historical K-factor.

ELO_K_ANNEALED_MINIMUM: Final = 6
# A quarter-base floor keeps annealed ratings from freezing completely.

ELO_MARGIN_VICTORY_SCALE: Final = 0.0
# The judge exposes confidence rather than score margin; map it only for
# explicit local scaling.

ELO_MARGIN_MULTIPLIER_CAP: Final = 5.0
# The reference schedule caps margin-scaled K at five times its base.

SINGLE_TURN_DEBATE_TURNS: Final = 1

ELO_UPSET_MARGIN: Final = 100

TOURNAMENT_MIN_MATCHES_PER_HYPOTHESIS: Final = 2
# One seeded match yields only two ratings; coverage floor and matchmaker must
# agree on two.

TOURNAMENT_MATCHES_PER_HYPOTHESIS: Final = 3
# Budget scales with the growing pool so late evolutionary children retain
# meaningful match records.

RANKING_WAVE_SIZE: Final = 12
# One durable wave adds no leases/writes but shares a snapshot; adaptation
# waits for wave boundaries.

RANKING_WAVE_MIN_SIZE: Final = 3
# Oversized bursts provoke throttling rather than speeding judgments.


# The failure sentinel must never become grounding content in downstream
# prompts.
LITERATURE_REVIEW_FAILED: Final = "__LIT_REVIEW_FAILED__"

LITERATURE_SYNTHESIS_FALLBACK_MAX_CHARS: Final = 8000
# Bound per-paper roll-up so failed synthesis cannot overflow a downstream
# prompt.


LOW_TEMPERATURE: Final = 0.3

MEDIUM_TEMPERATURE: Final = 0.5

HIGH_TEMPERATURE: Final = 0.7

# Batch reviews compare cheaply; large pools split calls to bound structured
# output.
COMPARATIVE_BATCH_THRESHOLD: Final = 5

# Only the rubric's non-viable band blocks; relative batch scoring otherwise
# always manufactures a low scorer.
NOT_VIABLE_SCORE: Final = 2

NEEDS_REVISION_SCORE: Final = 4

_NEUTRAL_SCORE: Final = 5
# Missing review scores are defects, never the worst score that silently
# disqualifies an idea.

MAX_CONCURRENT_LLM_CALLS: Final = 5

DEFAULT_MAX_ITERATIONS: Final = 1

DEFAULT_INITIAL_HYPOTHESES_COUNT: Final = 5

DEFAULT_EVOLUTION_MAX_COUNT: Final = 3

RESEARCH_OVERVIEW_TOP_K: Final = 10

DUPLICATE_SIMILARITY_THRESHOLD: Final = 0.95

# Progress names phases, not completion fractions; loop re-entry can repeat
# smaller values.
PROGRESS_SUPERVISOR_START: Final = 5
PROGRESS_SUPERVISOR_COMPLETE: Final = 10
PROGRESS_GENERATE_START: Final = 15
PROGRESS_GENERATE_COMPLETE: Final = 20
PROGRESS_REVIEW_START: Final = 25
PROGRESS_REVIEW_COMPLETE: Final = 40
PROGRESS_SAFETY_SCREEN_START: Final = 41
PROGRESS_SAFETY_SCREEN_COMPLETE: Final = 42
# Verification precedes tournament admission, so its progress band precedes
# ranking.
PROGRESS_DEEP_VERIFICATION_START: Final = 43
PROGRESS_DEEP_VERIFICATION_COMPLETE: Final = 44
PROGRESS_TOURNAMENT_START: Final = 55
PROGRESS_TOURNAMENT_COMPLETE: Final = 70
PROGRESS_ORCHESTRATOR_DECISION: Final = 75
PROGRESS_PROXIMITY_START: Final = 75
PROGRESS_PROXIMITY_COMPLETE: Final = 75
PROGRESS_META_REVIEW_START: Final = 80
PROGRESS_META_REVIEW_COMPLETE: Final = 82
PROGRESS_EVOLVE_START: Final = 85
PROGRESS_EVOLVE_COMPLETE: Final = 87
PROGRESS_RESEARCH_OVERVIEW_START: Final = 95
PROGRESS_RESEARCH_OVERVIEW_COMPLETE: Final = 99

# Dev mode overrides per-run paper counts with the smaller budget.
LITERATURE_REVIEW_PAPERS_COUNT: Final = 10

LITERATURE_REVIEW_PAPERS_COUNT_DEV: Final = 4

LITERATURE_REVIEW_RECENCY_YEARS: Final = 7

# Sources searched at once by one fan-out. Each source multiplies into several
# upstream requests (PubMed esearch, efetch, elink), so an unbounded fan-out
# bursts into upstream rate limits and leaves cancelled requests behind.
SOURCE_FANOUT: Final = 3


def distinct_queries(queries: Iterable[str]) -> list[str]:
    """Identical queries return identical results; spacing differences are not
    a different search."""
    seen: dict[str, str] = {}
    for query in queries:
        seen.setdefault(" ".join(query.split()), query)
    return list(seen.values())


LITERATURE_REVIEW_MAX_QUERIES: Final = 3
# Literature-node and per-hypothesis reflection query fan-outs share this
# bound.


def get_draft_max_iterations(hypotheses_count: int) -> int:
    """Reading needs base turns; cap per-hypothesis drafting work against
    runaway loops.
    """
    return min(5 + (hypotheses_count * 2), 30)


def get_validate_max_iterations(hypotheses_count: int) -> int:
    """Each hypothesis needs search, reading and refinement; cap the
    aggregate tool loop.
    """
    return min(hypotheses_count * 10, 50)


GENERATE_LIT_TOOL_MAX_PAPERS: Final = 3

VALIDATION_SYNTHESIS_BATCH_SIZE: Final = 3
# Smaller synthesis batches fit tight provider output limits at the cost of
# more parallel calls.


def truncate(text: str, limit: int = 200, suffix: str = "...") -> str:
    return text[:limit] + suffix if len(text) > limit else text


PROMPT_PAPER_MAX_CHARS: Final = 200_000

_PROMPT_TRUNCATION_MARKER: Final = "\n\n[... truncated for length ...]"


def truncate_for_prompt(text: str, max_chars: int = PROMPT_PAPER_MAX_CHARS) -> str:
    return truncate(text, max_chars, _PROMPT_TRUNCATION_MARKER)


def corpus_slug(research_goal: str) -> str:
    """Literature and tool-generation paths must derive the same slug or warm
    downloads miss.
    """
    return "research_" + hashlib.md5(research_goal.encode()).hexdigest()[:8]
