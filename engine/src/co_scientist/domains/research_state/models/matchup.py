import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any

from co_scientist.core.constants import INITIAL_ELO_RATING
from co_scientist.core.metrics import _known_field_kwargs


@dataclass(frozen=True)
class Matchup:
    """Checkpoints hold `to_dict()` of this. Older and partial checkpoints omit
    fields, so every field defaults and `from_dict` is the only legacy reader.
    """

    iteration: int = 0
    hypothesis_a: str | None = None
    hypothesis_b: str | None = None
    # Truncated text cannot identify an idea exactly; ids can.
    hypothesis_a_id: str | None = None
    hypothesis_b_id: str | None = None
    winner_id: str | None = None
    winner: str | None = None
    reasoning: str | None = None
    confidence: str | None = None
    tier: str | None = None
    criteria_comparisons: dict[str, str] = field(default_factory=dict)
    debate_turns: int = 1
    debate_transcript: list[dict[str, Any]] = field(default_factory=list)
    debate_verdict: str | None = None
    judge_model: str | None = None
    consensus_votes: list[str] = field(default_factory=list)
    position_balanced: bool = False
    invalid_output_fallback: bool = False
    winner_elo_before: int = INITIAL_ELO_RATING
    winner_elo_after: int = INITIAL_ELO_RATING
    loser_elo_before: int = INITIAL_ELO_RATING
    loser_elo_after: int = INITIAL_ELO_RATING

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Matchup":
        return cls(**_known_field_kwargs(cls, data))

    @property
    def loser_id(self) -> str | None:
        return (
            self.hypothesis_b_id if self.winner_id == self.hypothesis_a_id else self.hypothesis_a_id
        )

    @property
    def verdict(self) -> str:
        # Historical matchups lack verdict numbers; side a is idea 1.
        return str(self.debate_verdict or ("2" if self.winner == "b" else "1"))


# Strip only trailing verdicts; their numbers follow each turn's swapped order,
# while mid-text mentions may be protocol quotations.
_TRAILING_VERDICT_RE = re.compile(
    r"\s*better\s+(?:idea|hypothesis)\s*:\s*[12ab]\W*$",
    re.IGNORECASE,
)


def verdict_number(side: str) -> str:
    return "1" if side == "a" else "2"


def presented_first(entry: dict[str, Any]) -> str:
    """Each turn's numbers follow its presentation order; old entries without
    order metadata retain canonical order."""
    return "2" if str(entry.get("presentation_order") or "ab") == "ba" else "1"


def debate_transcript_document(transcript: list[dict[str, Any]], verdict: str) -> dict[str, Any]:
    """Persist the readable exchange and one verdict, excluding loop
    bookkeeping."""
    return {
        "verdict": verdict,
        "turns": [
            {
                "turn": int(entry.get("turn") or index),
                "favored": verdict_number(str(entry.get("winner") or "a")),
                "text": _TRAILING_VERDICT_RE.sub(
                    "", str(entry.get("reasoning") or "").rstrip()
                ).rstrip(),
                "first": presented_first(entry),
            }
            for index, entry in enumerate(transcript, 1)
        ],
    }
