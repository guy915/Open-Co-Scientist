from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Protocol

# Stop halving at the breadth floor; a single-question descent would be only a
# lookup.
DEFAULT_BREADTH_FLOOR = 2


@dataclass(frozen=True)
class ResearchBudget:
    depth: int = 2
    breadth: int = 4
    concurrency: int = 2
    hits_per_question: int = 4
    sources: tuple[str, ...] = ()
    breadth_floor: int = DEFAULT_BREADTH_FLOOR
    reserved_slots: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        for name in ("depth", "breadth", "concurrency", "hits_per_question"):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")
        if self.breadth_floor < 1:
            raise ValueError("breadth_floor must be at least 1")
        if not self.sources:
            raise ValueError("at least one source is required")
        self._check_reservations()

    def _check_reservations(self) -> None:
        """Unsearched sources or reservations consuming every slot are
        configuration mistakes, not no-ops.
        """
        total = 0
        for source, places in self.reserved_slots:
            if source not in self.sources:
                raise ValueError(f"reserved slots for unsearched {source!r}")
            if places < 1:
                raise ValueError(f"reserved slots for {source!r} must be >= 1")
            total += places
        if total >= self.hits_per_question and total:
            raise ValueError(f"reservations claim all {self.hits_per_question} hits")

    def descend(self) -> ResearchBudget | None:
        if self.depth <= 1:
            return None
        return replace(
            self,
            depth=self.depth - 1,
            breadth=max(self.breadth_floor, self.breadth // 2),
        )

    def max_threads(self) -> int:
        total = 0
        level: ResearchBudget | None = self
        while level is not None:
            total += level.breadth
            level = level.descend()
        return total


# Truncated SHA-256 balances run-scale identity against readable logs.
_ID_CHARS = 32

# Separate hashed parts to avoid concatenation collisions.
_SEP = b"\x1f"


def content_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256(kind.encode("utf-8"))
    for part in parts:
        digest.update(_SEP)
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()[:_ID_CHARS]


class CallStatus(Enum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"


class ThreadStatus(Enum):
    """DECLINED records budget refusal, not a failed research attempt."""

    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"
    DECLINED = "declined"


class StopReason(Enum):
    """Stop on an empty level; unavailable search must not fund follow-ups
    derived from nothing.
    """

    DEPTH_EXHAUSTED = "depth_exhausted"
    NO_RESULTS = "no_results"
    NO_FOLLOW_UPS = "no_follow_ups"


@dataclass(frozen=True)
class Question:
    text: str
    stance: str
    parent_id: str | None = None

    @property
    def id(self) -> str:
        return content_id("question", self.stance, self.text, self.parent_id or "")


@dataclass(frozen=True)
class SourceHit:
    """Preserve source ranking so a replay reproduces admission order."""

    locator: str
    title: str
    snippet: str
    rank: int
    score: float | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchCall:
    """Budget-refused hits remain recorded: seen-but-unread differs from
    never-seen.
    """

    question: str
    query: str
    source: str
    status: CallStatus
    hits: tuple[SourceHit, ...] = ()
    admitted: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    error: str | None = None
    duration_seconds: float | None = None

    @property
    def id(self) -> str:
        return content_id("call", self.source, self.question, self.query)


@dataclass(frozen=True)
class Finding:
    """Bind supporting span and provenance at extraction, rather than
    inventing them in the report.
    """

    text: str
    question: str
    locator: str
    span: str
    call_id: str

    @property
    def id(self) -> str:
        return content_id("finding", self.locator, self.span, self.question)


@dataclass(frozen=True)
class ThreadRecord:
    question: Question
    depth: int
    status: ThreadStatus
    call_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    follow_ups: tuple[str, ...] = ()
    summary: str | None = None
    note: str | None = None
    retry_breadth: int | None = None


@dataclass(frozen=True)
class ResearchResult:
    goal: str
    stances: tuple[str, ...]
    threads: tuple[ThreadRecord, ...]
    calls: tuple[SearchCall, ...]
    findings: tuple[Finding, ...]
    stop_reason: StopReason
    levels_run: int

    def declined(self) -> tuple[ThreadRecord, ...]:
        return tuple(t for t in self.threads if t.status is ThreadStatus.DECLINED)

    def summaries(self) -> tuple[str, ...]:
        return tuple(t.summary for t in self.threads if t.summary)


def dedupe_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    seen: set[str] = set()
    kept: list[Finding] = []
    for finding in findings:
        if finding.id in seen:
            continue
        seen.add(finding.id)
        kept.append(finding)
    return tuple(kept)


@dataclass(frozen=True)
class Document:
    hit: SourceHit
    text: str
    full_text: bool


@dataclass(frozen=True)
class ExtractedFinding:
    text: str
    locator: str
    span: str


@dataclass(frozen=True)
class Extraction:
    findings: tuple[ExtractedFinding, ...] = ()
    follow_ups: tuple[str, ...] = ()


class RetrievalError(Exception):
    def __init__(self, source: str, reason: str = "") -> None:
        self.source = source
        self.reason = reason
        super().__init__(f"{source}: {reason}" if reason else source)


class RetrievalPort(Protocol):
    async def search(self, *, query: str, source: str, limit: int) -> Sequence[SourceHit]: ...

    async def read(self, *, locator: str) -> str | None: ...


class ResearchModelPort(Protocol):
    async def plan_stances(self, *, goal: str, limit: int) -> Sequence[str]: ...

    async def ask_questions(self, *, goal: str, stance: str, limit: int) -> Sequence[str]: ...

    async def to_query(self, *, question: str) -> str:
        """Queries are lossy and source-shaped; retain the original question
        through broadening or retries.
        """
        ...

    async def extract(self, *, question: str, documents: Sequence[Document]) -> Extraction: ...

    async def compress(self, *, question: str, findings: Sequence[Finding]) -> str:
        """Summaries bound caller context while full findings remain in the
        ledger.
        """
        ...
