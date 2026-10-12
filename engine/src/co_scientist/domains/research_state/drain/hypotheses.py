from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, NamedTuple

import co_scientist.platform.retrieval.citations as citation_resolver
from co_scientist.core.config import settings
from co_scientist.domains.research_state.drain.reviews import (
    _CitationSink,
    _persist_engine_citations,
    _persist_engine_reviews,
)
from co_scientist.domains.research_state.elo import INITIAL_ELO
from co_scientist.domains.research_state.hypothesis_fields import store_text_fields
from co_scientist.domains.research_state.models import Hypothesis
from co_scientist.domains.research_state.repository import hypotheses as store
from co_scientist.domains.research_state.repository import records
from co_scientist.domains.research_state.repository.hypotheses import (
    HypothesisStateChanges,
    NewHypothesis,
)
from co_scientist.domains.research_state.repository.records import NewEvidence
from co_scientist.domains.research_state.text_utils import first_sentence
from co_scientist.platform.retrieval.citations import (
    CitationMetadata,
    Resolvability,
    Resolver,
    SourceType,
    classify_source_type,
    offline_resolver,
)

logger = logging.getLogger(__name__)

_PUBMED_URL_PMID = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")


@dataclass(frozen=True)
class ResolvedArticle:
    """Retraction and unresolvability remain distinct reader facts; preprint
    source type never gates evidence admission.
    """

    doi: str | None
    pmid: str | None
    available: bool
    retracted: bool = False
    source_type: str = SourceType.UNKNOWN.value


def _article_pmid(art: dict[str, Any]) -> str:
    if str(art.get("source") or "").lower() == "pubmed":
        source_id = str(art.get("source_id") or "").strip()
        if source_id.isdigit():
            return source_id
    match = _PUBMED_URL_PMID.search(str(art.get("url") or ""))
    return match.group(1) if match else ""


def _article_retracted(art: dict[str, Any]) -> bool:
    return bool(art.get("is_retracted")) or (
        str(art.get("correction_status") or "").lower() == "retracted"
    )


def _article_year(art: dict[str, Any]) -> int | None:
    try:
        return int(art["year"])
    except (KeyError, TypeError, ValueError):
        return None


def _configured_resolver() -> Resolver:
    if settings.evidence_resolver == "live":
        return citation_resolver.run_live_resolver()
    return offline_resolver


def resolve_articles(
    articles: list[dict[str, Any]],
) -> list[ResolvedArticle]:
    """Offline resolution uses metadata only; live resolution must finish
    before a write transaction opens.
    """
    metas = [
        CitationMetadata(
            url=str(art.get("url") or ""),
            doi=str(art.get("doi") or "").strip(),
            pmid=_article_pmid(art),
            retracted=_article_retracted(art),
            source=str(art.get("source") or ""),
            publication_type=str(art.get("publication_type") or ""),
            year=_article_year(art),
        )
        for art in articles
    ]
    verdicts = citation_resolver.resolve_many(metas, resolver=_configured_resolver())
    # Both metadata and independent resolver retractions retain their explicit
    # provenance rather than becoming plain unavailability.
    return [
        ResolvedArticle(
            doi=meta.doi or None,
            pmid=meta.pmid or None,
            available=verdict is Resolvability.RESOLVABLE,
            retracted=verdict is Resolvability.RETRACTED,
            source_type=classify_source_type(meta).value,
        )
        for meta, verdict in zip(metas, verdicts, strict=True)
    ]


@dataclass(frozen=True)
class _HypothesisSink:
    citations: _CitationSink
    store_id_by_engine_id: dict[str, str]
    persisted_engine_ids: set[str]


class _HypIdentity(NamedTuple):
    text: str
    title: str
    generation: int
    agent: str
    engine_id: str | None
    parent_id: str | None
    parent_ids: list[str] | None
    author: str


def _article_coalesced_fields(
    art: dict[str, Any],
) -> tuple[str, list[str], str]:
    url = art.get("url") or ""
    authors = art.get("authors") or []
    abstract = art.get("abstract") or ""
    return url, authors, abstract


def _persist_engine_evidence(
    run_id: str,
    articles: list[dict[str, Any]],
    resolved: list[ResolvedArticle],
    citations: _CitationSink,
    conn: sqlite3.Connection,
) -> None:
    """Citation maps carry no abstracts, so citation classification joins
    the corresponding evidence abstract by title.
    """
    ev_id_by_title = citations.ev_id_by_title
    abstract_by_title = citations.abstract_by_title
    for art, res in zip(articles, resolved, strict=True):
        url, authors, abstract = _article_coalesced_fields(art)
        ev_id = records.add_evidence(
            NewEvidence(
                run_id=run_id,
                title=art.get("title", "Untitled"),
                source=art.get("source", "engine"),
                url=url,
                authors=authors,
                year=art.get("year"),
                abstract=abstract,
                available=res.available,
                retracted=res.retracted,
                source_type=res.source_type,
                doi=res.doi,
                pmid=res.pmid,
                retrieved_at=art.get("retrieved_at"),
                retrieval_score=art.get("retrieval_score"),
                retrieval_rationale=art.get("retrieval_rationale"),
                retriever_version=art.get("retriever_version"),
                retrieval_call_id=art.get("retrieval_call_id"),
            ),
            conn=conn,
        )
        ev_id_by_title[art.get("title", "")] = ev_id
        abstract_by_title[art.get("title", "")] = abstract


def _persist_evidence_and_hypotheses(
    run_id: str,
    articles: list[dict[str, Any]],
    resolved: list[ResolvedArticle],
    hyps_parents_first: list[dict[str, Any]],
    sink: _HypothesisSink,
    conn: sqlite3.Connection,
) -> None:
    _persist_engine_evidence(run_id, articles, resolved, sink.citations, conn)
    for h in hyps_parents_first:
        _persist_engine_hypothesis(run_id, h, sink, conn)


def _derive_hypothesis_identity(h: dict[str, Any]) -> _HypIdentity:
    """Explicit lineage wins; pre-lineage checkpoints retain their
    evolution-history fallback.
    """
    text = h.get("text", "")
    title = _authored_title(h, text)
    engine_id = h.get("id") or None

    if "generation" in h or "parent_id" in h or "origin" in h:
        generation = int(h.get("generation", 0))
        parent_id = h.get("parent_id") or None
        agent = str(h.get("origin") or "generation")
    else:
        # Legacy pre-lineage checkpoints infer ancestry from evolution history.
        is_evolved = bool(h.get("evolution_history"))
        generation = 1 if is_evolved else 0
        parent_id = None
        agent = "evolution" if is_evolved else "generation"

    return _HypIdentity(
        text,
        title,
        generation,
        agent,
        engine_id,
        parent_id,
        _payload_parent_ids(h),
        _payload_author(h),
    )


def _payload_author(h: dict[str, Any]) -> str:
    """Scientist attribution travels in checkpointed enrichments rather than
    living solely in a mutable store row.
    """
    enrichments = h.get("enrichments")
    if not isinstance(enrichments, dict):
        return ""
    return str(enrichments.get("scientist_author") or "")


def _payload_parent_ids(h: dict[str, Any]) -> list[str] | None:
    raw = h.get("parent_ids")
    if not isinstance(raw, list):
        return None
    parent_ids = [pid for pid in raw if isinstance(pid, str) and pid]
    return parent_ids or None


# Deduplication archives redundant ideas; it must not appear as merit-based
# rejection.
DEDUPLICATED_REVIEW_DISPOSITION = "duplicate"


def _hypothesis_status(h: dict[str, Any]) -> str:
    """Deduplication is not merit rejection; undermined published ideas
    retain an explicit verification verdict beside active status.
    """
    if h.get("review_disposition") == DEDUPLICATED_REVIEW_DISPOSITION:
        return "duplicate"
    # Use the engine's admission predicate so tournament eligibility and
    # persisted publication status cannot drift.
    if not Hypothesis(
        text="",
        review_disposition=h.get("review_disposition"),
        deep_verification_verdict=h.get("deep_verification_verdict"),
    ).is_rankable():
        return "rejected"
    return "active"


def _mean_review_novelty(h: dict[str, Any]) -> float | None:
    """Novelty comes from its own review axis, not overall score; averaging
    prevents a single review from defining it.
    """
    validation = h.get("novelty_validation")
    if isinstance(validation, dict) and validation.get("decision") == "unknown":
        return None
    scores = [
        float(value)
        for review in h.get("reviews") or []
        if isinstance(review, dict)
        for value in [(review.get("scores") or {}).get("novelty")]
        if isinstance(value, (int, float)) and value
    ]
    return sum(scores) / len(scores) if scores else None


def _resolve_persisted_parent_id(
    identity: _HypIdentity, persisted_engine_ids: set[str]
) -> str | None:
    """Pruned parents cannot satisfy the foreign key; log the broken edge
    and store the child as a root.
    """
    parent_id = identity.parent_id
    if parent_id is not None and parent_id not in persisted_engine_ids:
        logger.warning(
            "hypothesis %s references pruned parent %s; storing as root",
            identity.engine_id,
            parent_id,
        )
        return None
    return parent_id


def _resolve_persisted_parent_ids(
    identity: _HypIdentity,
    parent_id: str | None,
    persisted_engine_ids: set[str],
) -> list[str] | None:
    """Multi-parent lineage retains the primary anchor and only persisted
    parents; an orphaned primary becomes a root.
    """
    if parent_id is None or not identity.parent_ids:
        return None
    kept = [pid for pid in identity.parent_ids if pid in persisted_engine_ids]
    if parent_id not in kept:
        kept.insert(0, parent_id)
    return kept if len(kept) > 1 else None


def _persist_engine_hypothesis_row(
    run_id: str,
    h: dict[str, Any],
    persisted_engine_ids: set[str],
    conn: sqlite3.Connection,
) -> tuple[str, str | None]:
    """Stable engine IDs persist end-to-end so matchups resolve by identity
    rather than fragile text prefixes.
    """
    identity = _derive_hypothesis_identity(h)
    parent_id = _resolve_persisted_parent_id(identity, persisted_engine_ids)
    parent_ids = _resolve_persisted_parent_ids(identity, parent_id, persisted_engine_ids)
    text_fields = store_text_fields({**h, "text": identity.text})
    hyp_id = store.add_hypothesis(
        NewHypothesis(
            run_id=run_id,
            title=identity.title,
            statement=text_fields["statement"],
            hypothesis_id=identity.engine_id,
            parent_id=parent_id,
            parent_ids=parent_ids,
            generation=identity.generation,
            # Cycle zero is real and must not collapse to an absent ordinal.
            creation_iteration=h.get("creation_iteration"),
            category=h.get("category") or None,
            mechanism=text_fields["mechanism"],
            expected_effect=text_fields["expected_effect"],
            experimental_context=text_fields["experimental_context"],
            introduction=h.get("introduction") or "",
            recent_findings=h.get("recent_findings") or "",
            safety_and_toxicity=h.get("safety_and_toxicity") or "",
            created_by_agent=identity.agent,
            author=identity.author,
        ),
        conn=conn,
    )
    store.update_hypothesis_state(
        hyp_id,
        HypothesisStateChanges(
            elo_rating=int(h.get("elo_rating", INITIAL_ELO)),
            win_delta=int(h.get("win_count", 0)),
            loss_delta=int(h.get("loss_count", 0)),
            novelty=_mean_review_novelty(h),
            clear_novelty=isinstance(h.get("novelty_validation"), dict)
            and h["novelty_validation"].get("decision") == "unknown",
            status=_hypothesis_status(h),
            verification_verdict=h.get("deep_verification_verdict"),
        ),
        conn=conn,
    )
    return hyp_id, identity.engine_id


def _persist_engine_hypothesis(
    run_id: str,
    h: dict[str, Any],
    sink: _HypothesisSink,
    conn: sqlite3.Connection,
) -> None:
    hyp_id, engine_id = _persist_engine_hypothesis_row(run_id, h, sink.persisted_engine_ids, conn)
    if engine_id:
        sink.store_id_by_engine_id[engine_id] = hyp_id
    _persist_engine_reviews(run_id, hyp_id, h, conn)
    _persist_engine_citations(run_id, hyp_id, h, sink.citations, conn)


def _hypotheses_with_proximity_archive(
    active: list[dict[str, Any]], removed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    archived: dict[str, dict[str, Any]] = {}
    for record in removed:
        hypothesis = record.get("hypothesis")
        if not isinstance(hypothesis, dict) or not hypothesis.get("id"):
            # Legacy text-only audit entries cannot safely reconstruct stable
            # identity or lineage.
            continue
        archived_hypothesis = dict(hypothesis)
        archived_hypothesis["review_disposition"] = "duplicate"
        archived[str(archived_hypothesis["id"])] = archived_hypothesis

    # Active rows win identity collisions; archives contain only ideas absent
    # from the active pool.
    by_id = dict(archived)
    by_id.update(
        {str(hypothesis["id"]): hypothesis for hypothesis in active if hypothesis.get("id")}
    )
    return list(by_id.values())


# Schema-less responses can exceed engine limits, so the persistence boundary
# independently enforces the title cap.
_TITLE_DISPLAY_CAP = 120


def _authored_title(h: dict[str, Any], text: str) -> str:
    """Legacy or malformed missing titles use the statement fallback;
    overlong authored titles are clipped rather than discarded.
    """
    raw = h.get("title")
    if isinstance(raw, str):
        authored = raw.strip()
        if authored:
            return authored[:_TITLE_DISPLAY_CAP]
    return first_sentence(text)
