from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence

from co_scientist.domains.research_state.claims.assessor import (
    _DEFAULT_RETRIEVAL_TOP_K,
    SENTENCE_SPLIT,
    AssessorDraft,
    EvidencePassage,
    deterministic_assessor,
    retrieve_passages,
)
from co_scientist.domains.research_state.claims.assessor import Assessor as Assessor
from co_scientist.domains.research_state.claims.assessor import as_passages as as_passages
from co_scientist.domains.research_state.claims.gate import (
    ClaimAssessment,
    EntailmentLabel,
    SupportSpan,
)
from co_scientist.domains.research_state.claims.gate import GateDecision as GateDecision
from co_scientist.domains.research_state.claims.gate import GateResult as GateResult
from co_scientist.domains.research_state.claims.gate import publication_gate as publication_gate
from co_scientist.platform.llm import record_deterministic_fallback

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
# Numeric references identify prompt positions before evidence IDs.
_POSITIONAL_KEY = re.compile(
    r"^[\[(]?\s*(?:passage|evidence)?\s*(\d{1,3})\s*[\])]?$", re.IGNORECASE
)
_QUOTE_NORMALIZE = str.maketrans(
    {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-"}  # noqa: RUF001
)


def locate_span(passage: EvidencePassage, quote: str) -> SupportSpan | None:
    """Normalization tolerates model typography; offsets and returned quotes
    always refer to the verbatim source.
    """
    normalized_quote = _WHITESPACE_RE.sub(" ", quote.translate(_QUOTE_NORMALIZE)).strip()
    if not normalized_quote:
        return None
    tokens = normalized_quote.split(" ")
    # Offsets always index the original source, never its normalized copy.
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    match = re.search(pattern, passage.text.translate(_QUOTE_NORMALIZE), flags=re.IGNORECASE)
    if match is None:
        return None
    start, end = match.start(), match.end()
    return SupportSpan(
        evidence_id=passage.evidence_id,
        quote=passage.text[start:end],
        start=start,
        end=end,
        source=passage.source,
        url=passage.url,
    )


def _downgrade_unproven_label(
    label: EntailmentLabel,
    supporting: list[SupportSpan],
    contradicting: list[SupportSpan],
) -> EntailmentLabel:
    """Every evidentiary verdict, including partial support and
    contradiction, needs a locatable source span.
    """
    if (label.is_supporting and not supporting) or (
        label is EntailmentLabel.CONTRADICTS and not contradicting
    ):
        return EntailmentLabel.INSUFFICIENT
    return label


def _normalize_key(cited_key: str) -> str:
    key = _WHITESPACE_RE.sub(" ", cited_key).strip()
    positional = _POSITIONAL_KEY.match(key)
    return positional.group(1) if positional else key.casefold()


def _passage_lookup(
    passages: Sequence[EvidencePassage],
    *,
    cites_evidence_ids: bool = False,
) -> dict[str, list[EvidencePassage]]:
    """Numeric references identify prompt positions, never potentially
    numeric evidence IDs. Parent aliases include every shown chunk.
    """
    # Import lazily: claims.chunking imports this package, creating a module
    # cycle.
    from co_scientist.domains.research_state.claims.chunking import parent_evidence_id

    lookup: dict[str, list[EvidencePassage]] = (
        {}
        if cites_evidence_ids
        else {str(position): [passage] for position, passage in enumerate(passages, start=1)}
    )
    for passage in passages:
        key = _normalize_key(passage.evidence_id)
        if cites_evidence_ids or not key.isdigit():
            lookup.setdefault(key, []).append(passage)
    exact_keys = set(lookup)
    for passage in passages:
        parent = _normalize_key(parent_evidence_id(passage.evidence_id))
        if (cites_evidence_ids or not parent.isdigit()) and parent not in exact_keys:
            lookup.setdefault(parent, []).append(passage)
    return lookup


def _resolve_span(
    cited_key: str,
    quote: str,
    lookup: dict[str, list[EvidencePassage]],
) -> SupportSpan | None:
    for passage in lookup.get(_normalize_key(cited_key), []):
        span = locate_span(passage, quote)
        if span is not None:
            return span
    return None


def _locate_all(
    cited: Sequence[tuple[str, str]],
    passages: Sequence[EvidencePassage],
    *,
    cites_evidence_ids: bool = False,
) -> list[SupportSpan]:
    """Unknown passage keys cannot borrow another claim's evidence; invalid
    spans cannot ground a verdict.
    """
    lookup = _passage_lookup(passages, cites_evidence_ids=cites_evidence_ids)
    spans: list[SupportSpan] = []
    dropped = 0
    for evidence_id, quote in cited:
        span = _resolve_span(evidence_id, quote, lookup)
        if span is None:
            dropped += 1
        else:
            spans.append(span)
    if dropped:
        logger.info(
            "%d cited span(s) could not be located in their cited source "
            "among %d shown passage(s); verdict unproven",
            dropped,
            len(passages),
        )
    return spans


BatchAssessor = Callable[
    [Sequence[str], Sequence[EvidencePassage]],
    Sequence["AssessorDraft | None"],
]

# Bound evidence context and split responses before provider calls.
_BATCH_EVIDENCE_CAP = 12

_BATCH_CLAIM_SPLIT = 20


def _round_robin_by_rank(
    per_claim_candidates: Sequence[Sequence[EvidencePassage]],
) -> list[EvidencePassage]:
    """A global cap must not let the first claim's low-ranked tail starve
    other claims' best passages.
    """
    max_rank = max((len(c) for c in per_claim_candidates), default=0)
    return [
        candidates[rank]
        for rank in range(max_rank)
        for candidates in per_claim_candidates
        if rank < len(candidates)
    ]


def _dedupe_capped(passages: Sequence[EvidencePassage], cap: int) -> list[EvidencePassage]:
    seen: set[str] = set()
    union: list[EvidencePassage] = []
    for passage in passages:
        if passage.evidence_id in seen:
            continue
        seen.add(passage.evidence_id)
        union.append(passage)
        if len(union) >= cap:
            break
    return union


def _assess_candidates(
    claim: str,
    draft: AssessorDraft,
    passages: Sequence[EvidencePassage],
    assessor_id: str,
    verification_method: str,
) -> ClaimAssessment:
    supporting = _locate_all(
        draft.supporting, passages, cites_evidence_ids=draft.cites_evidence_ids
    )
    contradicting = _locate_all(
        draft.contradicting, passages, cites_evidence_ids=draft.cites_evidence_ids
    )
    label = _downgrade_unproven_label(draft.label, supporting, contradicting)
    return ClaimAssessment(
        claim=claim,
        label=label,
        supporting_passages=tuple(supporting),
        contradicting_passages=tuple(contradicting),
        assessor=assessor_id,
        verification_method=verification_method,
    )


def _fallback_assessment(
    claim: str,
    candidates: Sequence[EvidencePassage],
    assessor_id: str,
) -> ClaimAssessment:
    """Requested assessor identity remains separate from the actual
    deterministic verification method.
    """
    draft = deterministic_assessor(claim, candidates)
    return _assess_candidates(claim, draft, candidates, assessor_id, draft.verification_method)


def assess_claims_batch(
    claims: Sequence[str],
    passages: Sequence[EvidencePassage],
    *,
    batch_assessor: BatchAssessor,
    assessor_id: str,
    top_k: int = _DEFAULT_RETRIEVAL_TOP_K,
) -> list[ClaimAssessment]:
    """Missing or malformed batch entries fall back individually, preserving
    valid neighboring assessments.
    """
    if not claims:
        return []
    if len(claims) > _BATCH_CLAIM_SPLIT:
        mid = (len(claims) + 1) // 2
        return assess_claims_batch(
            claims[:mid],
            passages,
            batch_assessor=batch_assessor,
            assessor_id=assessor_id,
            top_k=top_k,
        ) + assess_claims_batch(
            claims[mid:],
            passages,
            batch_assessor=batch_assessor,
            assessor_id=assessor_id,
            top_k=top_k,
        )
    return _assess_one_batch(
        claims,
        passages,
        batch_assessor=batch_assessor,
        assessor_id=assessor_id,
        top_k=top_k,
    )


def _assess_one_batch(
    claims: Sequence[str],
    passages: Sequence[EvidencePassage],
    *,
    batch_assessor: BatchAssessor,
    assessor_id: str,
    top_k: int,
) -> list[ClaimAssessment]:
    per_claim_candidates = [retrieve_passages(claim, passages, top_k=top_k) for claim in claims]
    union = _dedupe_capped(_round_robin_by_rank(per_claim_candidates), _BATCH_EVIDENCE_CAP)
    if not union:
        # No evidence means no provider call can establish grounding.
        drafts: Sequence[AssessorDraft | None] = [None] * len(claims)
    else:
        drafts = batch_assessor(claims, union)
    return [
        _assess_from_draft(
            claim,
            drafts[i] if i < len(drafts) else None,
            per_claim_candidates[i],
            union,
            assessor_id,
        )
        for i, claim in enumerate(claims)
    ]


def _assess_from_draft(
    claim: str,
    draft: AssessorDraft | None,
    own_candidates: Sequence[EvidencePassage],
    shown: Sequence[EvidencePassage],
    assessor_id: str,
) -> ClaimAssessment:
    if draft is None:
        if shown and assessor_id.startswith("llm:"):
            record_deterministic_fallback(assessor_id[4:], "claim_batch")
        return _fallback_assessment(claim, own_candidates, assessor_id)
    return _assess_candidates(claim, draft, shown, assessor_id, draft.verification_method)


_MIN_CLAIM_WORDS = 4

# Novelty and evidence-gap claims assert absence, not empirical entailment;
# treating
# them as contradictions can erase the pool.
_EVIDENCE_GAP_CLAIM = re.compile(
    r"""
      \bun(?:explored|examined|tested|studied|addressed|proven|reported
            |documented|characteri[sz]ed)\b
    | \bunder[-\ ]?(?:explored|examined|tested|studied|investigated
            |reported|documented|characteri[sz]ed)\b
    | \b(?:did|do|does)\ not\ (?:find|identify|locate|report|reveal)\b
    | \b(?:has|have|had)\ not\ been\b
    | \bnot\ (?:yet\ )?(?:been\ )?(?:\w+ly\ )?
        (?:tested|explored|examined|studied|addressed|established|proven
          |demonstrated|reported|characteri[sz]ed|investigated)\b
    | \bno\ (?:source|sources|study|studies|paper|papers|report|reports
          |trial|trials|prior\ work|published|evidence|citation|citations
          |data)\b
    | \bremains?\ to\ be\b
    | \bnever\ been\b
    | \bto\ (?:our|the)\ knowledge\b
    | \bgaps?\ in\ (?:the\ )?(?:literature|evidence|knowledge)\b
    | \bwithout\ access\ to\ a\ literature\ review\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_atomic_claims(text: str) -> list[str]:
    """Negative-existential novelty and evidence-gap claims belong in
    review, not empirical entailment checks.
    """
    claims: list[str] = []
    seen: set[str] = set()
    for raw in SENTENCE_SPLIT.split(text or ""):
        claim = raw.strip()
        if len(claim.split()) < _MIN_CLAIM_WORDS:
            continue
        if _EVIDENCE_GAP_CLAIM.search(claim):
            continue
        key = claim.lower()
        if key in seen:
            continue
        seen.add(key)
        claims.append(claim)
    return claims


_ASSESSOR_DETERMINISTIC = "deterministic-v1"


def assess_claim(
    claim: str,
    passages: Sequence[EvidencePassage],
    *,
    assessor: Assessor = deterministic_assessor,
    assessor_id: str = _ASSESSOR_DETERMINISTIC,
    top_k: int = _DEFAULT_RETRIEVAL_TOP_K,
) -> ClaimAssessment:
    """Evidence labels require exact source spans before they can count as
    grounded.
    """
    candidates = retrieve_passages(claim, passages, top_k=top_k)
    draft = assessor(claim, candidates)
    method = draft.verification_method if candidates else "no_evidence"
    return _assess_candidates(claim, draft, candidates, assessor_id, method)


__all__ = [
    "Assessor",
    "AssessorDraft",
    "BatchAssessor",
    "ClaimAssessment",
    "EntailmentLabel",
    "EvidencePassage",
    "GateDecision",
    "GateResult",
    "as_passages",
    "assess_claims_batch",
    "deterministic_assessor",
    "publication_gate",
    "retrieve_passages",
]
