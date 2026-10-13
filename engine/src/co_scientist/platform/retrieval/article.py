import dataclasses
from dataclasses import dataclass, field
from typing import Any

from co_scientist.core.metrics import _known_field_kwargs


@dataclass
class Article:
    title: str
    url: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    citations: int = 0
    abstract: str | None = None
    content: str | None = None
    source_id: str | None = None
    source: str = "pubmed"
    doi: str | None = None
    is_retracted: bool = False
    correction_status: str = "current"
    publication_type: str | None = None
    pdf_links: list[str] = field(default_factory=list)
    used_in_analysis: bool = False
    # Collection time differs from a downstream store's persistence timestamp.
    retrieved_at: float | None = None
    # Unranked/directly fetched legacy articles may lack hybrid scores and their
    # provenance.
    retrieval_score: float | None = None
    retrieval_rationale: str | None = None
    retriever_version: str | None = None
    # Deep-research articles retain the call/question that surfaced them;
    # ordinary searches may not.
    retrieval_call_id: str | None = None

    def __post_init__(self) -> None:
        # Sources serialize missing authors as null, and `.get("authors", [])`
        # keeps that None; prompt formatting slices this list.
        if self.authors is None:
            self.authors = []

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Article":
        """Ignore unknown keys so older serialized articles remain loadable."""
        return cls(**_known_field_kwargs(cls, data))
