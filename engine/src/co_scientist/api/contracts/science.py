from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, with_config
from typing_extensions import NotRequired, TypedDict


@with_config(ConfigDict(extra="allow"))
class Hypothesis(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    run_id: str
    parent_id: str | None
    generation: int
    category: str | None
    title: str
    statement: str
    mechanism: str | None
    expected_effect: str | None
    experimental_context: str | None
    created_by_agent: str
    author: NotRequired[str | None]
    created_at: float
    elo_rating: float
    win_count: int
    loss_count: int
    novelty_score: float | None
    plausibility_score: float | None
    testability_score: float | None
    safety_status: str | None
    status: str | None
    cluster_id: str | None
    unverified: NotRequired[bool]
    screened: NotRequired[bool]
    verification_verdict: NotRequired[str | None]


@with_config(ConfigDict(extra="allow"))
class HypothesisSnapshot(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    title: str
    statement: str


@with_config(ConfigDict(extra="allow"))
class ReferencedEvidence(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    title: str
    source: str
    url: str | None
    doi: NotRequired[str | None]
    pmid: NotRequired[str | None]
    sha256: NotRequired[str | None]


@with_config(ConfigDict(extra="allow"))
class Evidence(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    title: str
    source: str
    url: str
    authors: list[str]
    year: float | None
    abstract: NotRequired[str]
    available: bool
    retracted: bool


@with_config(ConfigDict(extra="allow"))
class MatchRow(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: int
    iteration: int
    winner_id: str
    loser_id: str
    winner_elo_before: float
    winner_elo_after: float
    loser_elo_before: float
    loser_elo_after: float
    rationale: str
    tier: str | None
    debate_turns: int
    debate_transcript: NotRequired[str | None]
    created_at: float


@with_config(ConfigDict(extra="allow"))
class SupportSpan(TypedDict):
    """Cited passage; older and curated spans can omit offsets/source."""

    evidence_id: str
    quote: str
    start: NotRequired[int]
    end: NotRequired[int]
    source: NotRequired[str]
    source_title: NotRequired[str]
    url: str


@with_config(ConfigDict(extra="allow"))
class ClaimEvidenceRow(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: int
    hypothesis_id: str
    claim: str
    label: str
    claim_role: NotRequired[str]
    supporting: list[SupportSpan]
    contradicting: list[SupportSpan]
    assessor: str
    verification_method: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class Review(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: int
    hypothesis_id: str
    reviewer_agent: str
    summary: str
    critique: str
    detail_json: NotRequired[str | None]
    novelty: float | None
    plausibility: float | None
    testability: float | None
    overall: float | None


@with_config(ConfigDict(extra="allow"))
class SafetyDecision(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: int
    stage: str
    decision: Literal["allow"] | Literal["redact"] | Literal["hold"] | Literal["block"]
    reason: str
    matches: list[str]
    category: NotRequired[str | None]
    policy_version: NotRequired[str | None]
    risk_domains: list[str]
    requires_review: bool
    assessor: NotRequired[str | None]
    resolution: NotRequired[Literal["approved"] | Literal["rejected"] | None]
    resolved_by: NotRequired[str | None]
    resolved_at: NotRequired[float | None]
