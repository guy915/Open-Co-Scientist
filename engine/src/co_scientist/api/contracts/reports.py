from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, with_config
from typing_extensions import NotRequired, TypedDict

from co_scientist.api.contracts.common import RunMode
from co_scientist.api.contracts.science import ClaimEvidenceRow


@with_config(ConfigDict(extra="allow"))
class LeaderboardEntry(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    title: str
    elo: float


@with_config(ConfigDict(extra="allow"))
class IdeaBuckets(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    high_potential: list[IdeaBucketEntry]
    non_viable: list[IdeaBucketEntry]


@with_config(ConfigDict(extra="allow"))
class RetrievalDegradation(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    reason: str
    lost: list[str]
    floor: str


@with_config(ConfigDict(extra="allow"))
class ReportPayload(TypedDict):
    """Persisted synthesis; old reports can omit every modern section."""

    research_goal: NotRequired[str]
    run_mode: NotRequired[RunMode]
    provider: NotRequired[str]
    hypothesis_count: NotRequired[int]
    idea_count: NotRequired[int]
    verified_count: NotRequired[int]
    evidence_count: NotRequired[int]
    match_count: NotRequired[int]
    citation_summary: NotRequired[dict[str, float]]
    leaderboard: NotRequired[list[LeaderboardEntry]]
    meta_review: NotRequired[dict[str, Any]]
    research_overview: NotRequired[ResearchOverview]
    knowledge_base: NotRequired[list[KnowledgeBaseTopic]]
    agent_insights: NotRequired[AgentInsights]
    idea_buckets: NotRequired[IdeaBuckets]
    claim_evidence: NotRequired[list[ClaimEvidenceRow]]
    execution_time: NotRequired[float]
    degraded_sections: NotRequired[list[str]]
    retrieval_degradation: NotRequired[RetrievalDegradation | None]


@with_config(ConfigDict(extra="allow"))
class KnowledgeBaseTopic(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    title: str
    summary: str
    detail: str
    uncertainty: NotRequired[str]
    reference_ids: list[str]


@with_config(ConfigDict(extra="allow"))
class RecommendedDirection(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    focus_area: str
    recommendation: str
    justification: str


@with_config(ConfigDict(extra="allow"))
class AgentInsights(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    key_findings: NotRequired[list[str]]
    uncertainties: NotRequired[list[str]]
    contradictions: NotRequired[list[str]]
    recommended_directions: NotRequired[list[RecommendedDirection]]
    next_experiments: NotRequired[list[str]]


@with_config(ConfigDict(extra="allow"))
class IdeaBucketEntry(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    title: str
    reason: str


@with_config(ConfigDict(extra="allow"))
class ResearchSubTopic(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    title: NotRequired[str]
    why: NotRequired[str]
    what: NotRequired[str]
    example_idea: NotRequired[str]
    specific_questions: NotRequired[list[str]]


@with_config(ConfigDict(extra="allow"))
class ResearchDirection(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    title: str
    importance: str
    suggested_experiments: list[str]
    recent_findings: NotRequired[str]
    sub_topics: NotRequired[list[ResearchSubTopic]]


@with_config(ConfigDict(extra="allow"))
class OverviewSummary(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    summary: NotRequired[str]
    research_directions: NotRequired[list[ResearchDirection]]


@with_config(ConfigDict(extra="allow"))
class SpecificAim(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    overarching_goal: NotRequired[str]
    hypothesis: NotRequired[str]
    reasoning: NotRequired[str]
    aim: NotRequired[str]
    rationale: NotRequired[str]
    approach: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class SpecificAims(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    disease_description: NotRequired[str]
    unmet_need: NotRequired[str]
    proposed_solution: NotRequired[str]
    aims: NotRequired[list[SpecificAim]]
    pilot_evaluation: NotRequired[str]
    introduction: NotRequired[str]
    impact: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class ResearchContact(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    candidate_id: str
    name: str
    expertise: str
    justification: str
    source_id: str
    source_title: str
    source_url: str
    source: str
    research_direction: NotRequired[str]


@with_config(ConfigDict(extra="allow"))
class ResearchOverview(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    overview: NotRequired[OverviewSummary]
    nih_specific_aims: NotRequired[SpecificAims]
    research_contacts: NotRequired[list[ResearchContact]]


@with_config(ConfigDict(extra="allow"))
class Report(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    id: str
    run_id: str
    payload: ReportPayload
    markdown_path: str
    markdown_text: NotRequired[str]
    created_at: float
