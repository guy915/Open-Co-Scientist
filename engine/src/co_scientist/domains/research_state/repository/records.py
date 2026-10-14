from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, fields
from typing import Any

from co_scientist.domains.research_state.claims.gate import DEFAULT_CLAIM_ROLE, ClaimEdge
from co_scientist.platform.db import connect, current_time, list_by_run, transaction, use_conn
from co_scientist.platform.db.storage_admission import check_run_input_storage, current_peer
from co_scientist.platform.retrieval.citations import CitationState


@dataclass(frozen=True)
class NewEvidence:
    """Passage offsets index insert-time title plus abstract; direct corpus
    fetches and uploads legitimately have no retrieval call.
    """

    run_id: str
    title: str
    source: str = "mock"
    url: str = ""
    authors: Iterable[str] | None = None
    year: int | None = None
    abstract: str = ""
    available: bool = True
    retracted: bool = False
    source_type: str = ""
    mime_type: str | None = None
    sha256: str | None = None
    byte_size: int | None = None
    document_version: str | None = None
    extraction_tool: str | None = None
    doi: str | None = None
    pmid: str | None = None
    retrieved_at: float | None = None
    retrieval_score: float | None = None
    retrieval_rationale: str | None = None
    retriever_version: str | None = None
    retrieval_call_id: str | None = None


def _evidence_passage_text(f: NewEvidence) -> str:
    """Match the grounding passage formula exactly so persisted span offsets
    never drift from their evidence text.
    """
    return " ".join(str(part or "") for part in (f.title, f.abstract)).strip()


def add_evidence(
    evidence: NewEvidence,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    ev_id = str(uuid.uuid4())
    values = _record_columns(evidence, {"authors": "authors_json"})
    values.update(
        id=ev_id,
        authors_json=json.dumps(list(evidence.authors or [])),
        available=1 if evidence.available else 0,
        retracted=1 if evidence.retracted else 0,
        source_type=evidence.source_type or None,
        passage_text=_evidence_passage_text(evidence),
    )
    if evidence.source == "attachment":
        with nullcontext(conn) if conn is not None else transaction(db_path) as active:
            size = sum(
                len(str(values[key]).encode()) for key in ("abstract", "passage_text", "title")
            )
            check_run_input_storage(active, evidence.run_id, "corpus", size)
            values["peer_hash"] = current_peer()
            _insert_record("evidence", values, db_path, active)
    else:
        _insert_record("evidence", values, db_path, conn)
    return ev_id


def list_evidence(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    rows = list_by_run("evidence", run_id, db_path, conn, json_fields=("authors",))
    for row in rows:
        row.pop("peer_hash", None)
        row["available"] = bool(row["available"])
        row["retracted"] = bool(row["retracted"])
    return rows


@dataclass(frozen=True)
class NewCitation:
    run_id: str
    hypothesis_id: str
    evidence_id: str
    claim: str
    state: CitationState


def add_citation(citation: NewCitation, *, conn: sqlite3.Connection | None = None) -> None:
    values = _record_columns(citation)
    values["state"] = citation.state.value
    _insert_record("citations", values, None, conn)


def list_citations(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return list_by_run("citations", run_id, db_path, conn)


@dataclass(frozen=True)
class NewClaimEvidence:
    """Legacy spans may be plain strings; verification provenance never
    upgrades scientific authority, and absent provenance stays unknown.
    """

    run_id: str
    hypothesis_id: str
    claim: str
    label: str
    supporting: Iterable[Any]
    contradicting: Iterable[Any]
    assessor: str
    claim_role: str = DEFAULT_CLAIM_ROLE
    verification_method: str = "legacy_unknown"


def add_claim_evidence(
    edge: NewClaimEvidence,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    values = _record_columns(
        edge,
        {
            "supporting": "supporting_json",
            "contradicting": "contradicting_json",
        },
    )
    values.update(
        supporting_json=json.dumps(list(edge.supporting)),
        contradicting_json=json.dumps(list(edge.contradicting)),
    )
    _insert_record("claim_evidence", values, db_path, conn)


def list_claim_evidence(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return list_by_run(
        "claim_evidence",
        run_id,
        db_path,
        conn,
        json_fields=("supporting", "contradicting"),
    )


def list_claim_edges(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[ClaimEdge]:
    return [ClaimEdge.from_row(row) for row in list_claim_evidence(run_id, db_path, conn)]


@dataclass(frozen=True)
class NewReview:
    """Structured review detail is display-only; scientist authorship and
    verdict must not be inferred from summary prose.
    """

    run_id: str
    hypothesis_id: str
    reviewer_agent: str
    summary: str
    critique: str
    novelty: float | None = None
    plausibility: float | None = None
    testability: float | None = None
    overall: float | None = None
    author: str = ""
    verdict: str | None = None
    detail_json: str | None = None


def add_review(
    review: NewReview,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    _insert_record("reviews", _record_columns(review), db_path, conn)


def list_reviews(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return list_by_run("reviews", run_id, db_path, conn)


def review_exists(review_id: int, conn: sqlite3.Connection | None = None) -> bool:
    """AUTOINCREMENT never reuses deleted review IDs, so carried scientist
    reviews can safely test whether their own row survived reset.
    """
    with use_conn(conn, None) as conn:
        row = conn.execute("SELECT 1 FROM reviews WHERE id=?", (review_id,)).fetchone()
        return row is not None


@dataclass(frozen=True)
class NewMatch:
    """Absent transcripts mean the judge recorded no turns; legacy depth one
    represents a single comparison.
    """

    run_id: str
    iteration: int
    winner_id: str
    loser_id: str
    winner_before: int
    winner_after: int
    loser_before: int
    loser_after: int
    rationale: str
    tier: str | None = None
    debate_turns: int = 1
    debate_transcript: str | None = None


def add_match(
    match: NewMatch,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    values = _record_columns(
        match,
        {
            f"{side}_{boundary}": f"{side}_elo_{boundary}"
            for side in ("winner", "loser")
            for boundary in ("before", "after")
        },
    )
    _insert_record("matches", values, db_path, conn)


def list_matches(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return list_by_run("matches", run_id, db_path, conn)


@dataclass(frozen=True)
class NewProximityEdge:
    run_id: str
    source_hypothesis_id: str
    target_hypothesis_id: str
    similarity: float
    degree: str | None = None
    cluster_id: str | None = None
    method: str | None = None
    version: str | None = None
    model: str | None = None
    updated_at: float | None = None


def add_proximity_edge(edge: NewProximityEdge, *, conn: sqlite3.Connection | None = None) -> None:
    _insert_record("proximity_edges", _record_columns(edge), None, conn)


@dataclass(frozen=True)
class NewSafetyDecision:
    run_id: str
    stage: str
    decision: str
    reason: str
    matches: list[str]
    category: str | None = None
    policy_version: str | None = None
    risk_domains: list[str] | None = None
    requires_review: bool = False
    assessor: str | None = None


def add_safety_decision(
    decision: NewSafetyDecision,
    *,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    values = _record_columns(
        decision,
        {"matches": "matches_json", "risk_domains": "risk_domains_json"},
    )
    values.update(
        matches_json=json.dumps(decision.matches),
        risk_domains_json=json.dumps(decision.risk_domains or []),
        requires_review=1 if decision.requires_review else 0,
    )
    _insert_record("safety_decisions", values, db_path, conn)


_NewRecord = (
    NewEvidence
    | NewCitation
    | NewClaimEvidence
    | NewReview
    | NewMatch
    | NewProximityEdge
    | NewSafetyDecision
)


def _record_columns(record: _NewRecord, aliases: dict[str, str] | None = None) -> dict[str, Any]:
    aliases = aliases or {}
    return {
        aliases.get(field.name, field.name): getattr(record, field.name) for field in fields(record)
    }


def _insert_record(
    table: str,
    values: dict[str, Any],
    db_path: str | None,
    conn: sqlite3.Connection | None,
) -> None:
    with use_conn(conn, db_path) as active:
        values["created_at"] = current_time()
        columns = ", ".join(values)
        placeholders = ",".join("?" for _ in values)
        active.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )


def list_safety_decisions(
    run_id: str,
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    rows = list_by_run(
        "safety_decisions",
        run_id,
        db_path,
        conn,
        json_fields=("matches", "risk_domains"),
    )
    for row in rows:
        row["requires_review"] = bool(row.get("requires_review"))
    return rows


def count_unresolved_review_decisions(
    run_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """This run-detail hot path needs one indexed count, not full safety-row
    JSON decoding.
    """
    with use_conn(conn, None) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM safety_decisions WHERE run_id=? AND "
            "requires_review=1 AND resolution IS NULL",
            (run_id,),
        ).fetchone()
        return int(row[0]) if row else 0


def resolve_safety_decision(
    run_id: str,
    decision_id: int,
    resolution: str,
    resolved_by: str,
) -> bool:
    if resolution not in {"approved", "rejected"}:
        raise ValueError("resolution must be approved or rejected")
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE safety_decisions SET resolution=?, resolved_by=?, "
            "resolved_at=? WHERE id=? AND run_id=? AND requires_review=1 "
            "AND resolution IS NULL",
            (resolution, resolved_by, current_time(), decision_id, run_id),
        )
        return cursor.rowcount == 1


def safety_stage_is_approved(
    run_id: str,
    stage: str,
    policy_version: str,
    db_path: str | None = None,
) -> bool:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT resolution FROM safety_decisions WHERE run_id=? AND "
            "stage=? AND policy_version=? ORDER BY id DESC LIMIT 1",
            (run_id, stage, policy_version),
        ).fetchone()
    return bool(row and row["resolution"] == "approved")
