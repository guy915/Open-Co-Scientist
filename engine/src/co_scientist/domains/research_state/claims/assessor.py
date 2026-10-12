from __future__ import annotations

import dataclasses
import functools
import re
from collections.abc import Callable, Sequence

from co_scientist.core.text_matching import coverage, tokenize
from co_scientist.domains.research_state.claims.gate import EntailmentLabel

RETRIEVAL_STOPWORDS = frozenset(
    {
        "a",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "did",
        "do",
        "for",
        "few",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "or",
        "our",
        "out",
        "own",
        "per",
        "she",
        "so",
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
        "to",
        "too",
        "up",
        "us",
        "via",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "yet",
        "you",
        "your",
    }
)


@dataclasses.dataclass(frozen=True)
class EvidencePassage:
    """Offsets are measured against the exact passage text used for
    verification.
    """

    evidence_id: str
    text: str
    source: str = ""
    url: str = ""


def as_passages(texts: Sequence[str]) -> list[EvidencePassage]:
    return [EvidencePassage(evidence_id=f"passage-{i}", text=t) for i, t in enumerate(texts)]


# Extraction and evidence chunking share sentence boundaries so quoted offsets
# remain
# consistent.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_CONTRADICTION_MARKERS = (
    "no evidence",
    "not associated",
    "no association",
    "does not",
    "did not",
    "failed to",
    "contrary to",
    "contradicts",
    "refutes",
    "disproven",
    "no significant",
    "not significant",
    "ineffective",
    "statistically equivalent",
    "remained equivalent",
    "waned to",
    "returned to baseline",
    "pre-vaccination baseline",
)

# Concept expansion improves recall only; provenance and polarity still decide
# support.
_CONCEPT_ALIASES = {
    "antagonism": "block",
    "antagonist": "block",
    "blocking": "block",
    "blockade": "block",
    "inhibiting": "inhibit",
    "inhibition": "inhibit",
    "pharmacological": "drug",
    "compound": "drug",
    "agent": "drug",
    "immunization": "vaccine",
    "immunosurveillance": "surveillance",
    "antitumor": "tumor",
    "antibody": "antibody",
    "titers": "response",
    "responses": "response",
    "durable": "persist",
    "survival": "survival",
    "glioblastoma": "glioma",
    "leukemic": "aml",
    "myeloid": "aml",
    "malignancy": "tumor",
    "malignancies": "tumor",
    "proliferation": "growth",
    "curtailed": "reduce",
    "reduces": "reduce",
    "reduced": "reduce",
    "restores": "restore",
    "restored": "restore",
    "established": "restore",
    "eradicated": "effective",
    "effective": "effective",
    "pathogen": "microorganism",
    "infected": "infection",
}

# Offline support requires majority overlap to prevent topically related
# passages from
# establishing unrelated claims.
_SUPPORT_LEXICAL_THRESHOLD = 0.50

# Partial support admits weaker overlap; bounded recall and shared concepts
# limit
# unrelated matches.
_PARTIAL_LEXICAL_THRESHOLD = 0.25

# One shared topical word is insufficient to establish a meaningful relation.
_PARTIAL_MIN_SHARED_TOKENS = 2

# A negation must cover the claim before it can contradict it.
_MIN_CONTRADICTION_COVERAGE = _PARTIAL_LEXICAL_THRESHOLD

_DEFAULT_RETRIEVAL_TOP_K = 5


@dataclasses.dataclass(frozen=True)
class AssessorDraft:
    """Prompt passage positions and durable evidence IDs occupy separate
    namespaces.
    """

    label: EntailmentLabel
    supporting: tuple[tuple[str, str], ...] = ()
    contradicting: tuple[tuple[str, str], ...] = ()
    verification_method: str = "legacy_unknown"
    cites_evidence_ids: bool = True


Assessor = Callable[[str, Sequence[EvidencePassage]], AssessorDraft]


# Short noise is excluded without discarding meaningful domain acronyms.
_SHORT_TOKEN_EXCEPTIONS = frozenset({"aml"})


@functools.lru_cache(maxsize=256)
def _tokens(text: str) -> frozenset[str]:
    """Repeated claim retrieval shares tokenization without changing
    verifier semantics.
    """
    tokens: set[str] = set()
    for token in tokenize(text):
        if len(token) <= 3 and token not in _SHORT_TOKEN_EXCEPTIONS:
            continue
        tokens.add(_CONCEPT_ALIASES.get(token, token))
    return frozenset(tokens)


def _lexical_score(claim: str, passage: str) -> float:
    """Lexical overlap is topical recall, not semantic entailment; long
    passages must not dilute short claims arbitrarily.
    """
    a, b = _tokens(claim), _tokens(passage)
    return coverage(a, b)


@functools.lru_cache(maxsize=256)
def _retrieval_tokens(text: str) -> frozenset[str]:
    """Recall expansion selects candidate passages; it never supplies an
    entailment verdict.
    """
    short = {token for token in tokenize(text) if len(token) <= 3 and not token.isdigit()}
    return (_tokens(text) | short) - RETRIEVAL_STOPWORDS


def retrieve_passages(
    claim: str,
    passages: Sequence[EvidencePassage],
    *,
    top_k: int = _DEFAULT_RETRIEVAL_TOP_K,
) -> list[EvidencePassage]:
    """Per-claim bounds prevent run-wide topical coincidence from
    masquerading as grounding; ties retain input order.
    """
    query = _retrieval_tokens(claim)
    scored = [(i, p, len(query & _retrieval_tokens(p.text))) for i, p in enumerate(passages)]
    relevant = [(i, p, s) for i, p, s in scored if s > 0.0]
    relevant.sort(key=lambda t: (-t[2], t[0]))
    return [p for _, p, _ in relevant[: max(0, top_k)]]


def _quote_negates_claim(claim: str, quote: str) -> bool:
    """A contradiction must cover the claim's subject and negate it within
    the quoted span itself.
    """
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return False
    coverage = len(claim_tokens & _tokens(quote)) / len(claim_tokens)
    if coverage < _MIN_CONTRADICTION_COVERAGE:
        return False
    lowered = quote.lower()
    return any(marker in lowered for marker in _CONTRADICTION_MARKERS)


def _contradicting_sentence(claim: str, text: str) -> str | None:
    """Negation elsewhere in a passage cannot refute the claim; the selected
    quote must do so.
    """
    negating = [
        sentence for sentence in _split_sentences(text) if _quote_negates_claim(claim, sentence)
    ]
    if not negating:
        return None
    return max(negating, key=lambda s: _lexical_score(claim, s))


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if s.strip()]


def _best_sentence(claim: str, text: str) -> str:
    sentences = _split_sentences(text)
    if not sentences:
        return text.strip()
    return max(sentences, key=lambda s: _lexical_score(claim, s))


def _classify_passage(
    claim: str,
    passage: EvidencePassage,
    support_threshold: float,
    partial_threshold: float,
) -> tuple[str, tuple[str, str]] | None:
    score = _lexical_score(claim, passage.text)
    if score >= support_threshold / 2:
        negation = _contradicting_sentence(claim, passage.text)
        if negation is not None:
            return "contradicts", (passage.evidence_id, negation)
    if score >= support_threshold:
        quote = _best_sentence(claim, passage.text)
        return "supports", (passage.evidence_id, quote)
    shared = len(_tokens(claim) & _tokens(passage.text))
    if score >= partial_threshold and shared >= _PARTIAL_MIN_SHARED_TOKENS:
        quote = _best_sentence(claim, passage.text)
        return "partial", (passage.evidence_id, quote)
    return None


def _entailment_label(
    supporting: Sequence[tuple[str, str]],
    partial: Sequence[tuple[str, str]],
    contradicting: Sequence[tuple[str, str]],
) -> EntailmentLabel:
    if contradicting:
        return EntailmentLabel.CONTRADICTS
    if supporting:
        return EntailmentLabel.SUPPORTS
    if partial:
        return EntailmentLabel.PARTIAL
    return EntailmentLabel.INSUFFICIENT


def deterministic_assessor(
    claim: str,
    passages: Sequence[EvidencePassage],
    *,
    support_threshold: float = _SUPPORT_LEXICAL_THRESHOLD,
    partial_threshold: float = _PARTIAL_LEXICAL_THRESHOLD,
) -> AssessorDraft:
    """Offline lexical judgments are deterministic stand-ins, not scientific
    validation; they still require exact provenance.
    """
    supporting: list[tuple[str, str]] = []
    partial: list[tuple[str, str]] = []
    contradicting: list[tuple[str, str]] = []
    for passage in passages:
        classified = _classify_passage(claim, passage, support_threshold, partial_threshold)
        if classified is None:
            continue
        kind, entry = classified
        if kind == "contradicts":
            contradicting.append(entry)
        elif kind == "supports":
            supporting.append(entry)
        else:
            partial.append(entry)

    return AssessorDraft(
        label=_entailment_label(supporting, partial, contradicting),
        supporting=tuple(supporting) + tuple(partial),
        contradicting=tuple(contradicting),
        verification_method="deterministic_lexical" if passages else "no_evidence",
        cites_evidence_ids=True,
    )
