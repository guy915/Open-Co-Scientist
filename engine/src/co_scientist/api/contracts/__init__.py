from typing_extensions import TypedDict

from co_scientist.api.contracts.runs import Run, RunMessage
from co_scientist.api.contracts.science import (
    ClaimEvidenceRow,
    Evidence,
    Hypothesis,
    MatchRow,
    Review,
    SafetyDecision,
)


class RunsResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    runs: list[Run]


class HypothesesResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    hypotheses: list[Hypothesis]


class EvidenceResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    evidence: list[Evidence]


class MatchesResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    matches: list[MatchRow]


class ReviewsResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    reviews: list[Review]


class SafetyResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    safety: list[SafetyDecision]


class ClaimsResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    claim_evidence: list[ClaimEvidenceRow]


class MessagesResponse(TypedDict):
    """Named collection envelope returned by the HTTP API."""

    messages: list[RunMessage]
