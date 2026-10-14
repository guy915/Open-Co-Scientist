"""The order used by every reader-facing hypothesis collection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypeVar

from co_scientist.core.constants import INITIAL_ELO_RATING


class PublicationHypothesis(Protocol):
    @property
    def elo_rating(self) -> int: ...

    @property
    def total_matches(self) -> int: ...

    def is_undermined(self) -> bool: ...


_Hypothesis = TypeVar("_Hypothesis", bound=PublicationHypothesis | Mapping[str, Any])


def _publication_key(
    hypothesis: PublicationHypothesis | Mapping[str, Any],
) -> tuple[bool, bool, int]:
    if isinstance(hypothesis, Mapping):
        rating = hypothesis.get("elo_rating")
        if rating is None:
            rating = INITIAL_ELO_RATING
        return (
            hypothesis.get("verification_verdict") == "undermined",
            not (int(hypothesis.get("win_count") or 0) + int(hypothesis.get("loss_count") or 0)),
            -int(rating),
        )
    return (hypothesis.is_undermined(), not hypothesis.total_matches, -hypothesis.elo_rating)


def rank_for_publication(hypotheses: list[_Hypothesis]) -> list[_Hypothesis]:
    """Demote undermined and unplayed ideas; preserve input order on Elo ties."""
    return sorted(hypotheses, key=_publication_key)
