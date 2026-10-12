from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import ConfigDict, with_config
from typing_extensions import NotRequired, TypeAliasType, TypedDict

from co_scientist.platform.db.models import RunStatus as RunStatus

RunMode: TypeAlias = Literal["standard", "express", "extended", "ultra"]


RunFocus: TypeAlias = (
    Literal["prefer_evidence"]
    | Literal["balance"]
    | Literal["prefer_novelty"]
    | Literal["breakthrough"]
)


RunTier: TypeAlias = (
    Literal["express"] | Literal["standard"] | Literal["extended"] | Literal["ultra"]
)


RunEventActivity: TypeAlias = Literal[
    "planning",
    "literature_search",
    "drafting",
    "review",
    "tournament",
    "evolution",
    "deduplication",
    "safety",
    "synthesis",
    "other",
]


@with_config(ConfigDict(extra="allow"))
class NamedCriterion(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    name: str
    value: str


RunCriterion: TypeAlias = str | NamedCriterion


AttributeScale = TypedDict(
    "AttributeScale",
    {"1": NotRequired[str], "3": NotRequired[str], "5": NotRequired[str]},
)
with_config(ConfigDict(extra="allow"))(AttributeScale)


@with_config(ConfigDict(extra="allow"))
class ScaledAttribute(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    name: str
    scale: AttributeScale


@with_config(ConfigDict(extra="allow"))
class CategoricalAttribute(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    name: str
    values: list[str]


RunAttribute: TypeAlias = str | ScaledAttribute | CategoricalAttribute


@with_config(ConfigDict(extra="allow"))
class RunSetupConfig(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    goal: str
    requirements: list[str]
    attributes: list[RunAttribute]
    criteria: list[RunCriterion]
    focus: RunFocus
    tier: RunTier


JsonPrimitive: TypeAlias = str | float | bool | None

JsonValue = TypeAliasType("JsonValue", "JsonPrimitive | list[JsonValue] | dict[str, JsonValue]")


@with_config(ConfigDict(extra="allow", json_schema_extra={"x-open-config": True}))
class RunConfig(TypedDict):
    """JSON contract; omitted fields stay omitted."""

    initial_hypotheses_count: NotRequired[int]
    max_iterations: NotRequired[int]
    evolution_max_count: NotRequired[int]
    tournament_pairs: NotRequired[int]
    evidence_count: NotRequired[int]
    enable_literature_review: NotRequired[bool]
    enable_web_search: NotRequired[bool]
    k_factor: NotRequired[int]
    tier: NotRequired[RunTier]
    focus: NotRequired[RunFocus]
    setup: NotRequired[RunSetupConfig]
