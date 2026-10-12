"""Budget exhaustion must not strand admitted ideas without review. Forced
attempts are spent on issue, with per-idea and run-wide ceilings."""

from collections.abc import Iterable

from co_scientist.domains.research_state.models import Hypothesis, has_peer_review

# Checkpointed issuance prevents a resumed run from reopening a spent override.
OWED_REVIEW_MARKER = "owed_review_issued"

# Growing admission pools must not bypass the run-wide override ceiling.
MAX_OWED_REVIEW_OVERRIDES_PER_RUN = 24

# The app archives duplicates without removing them; buying review for an
# excluded idea adds nothing. Mirror its marker without an engine-to-app import.
_ARCHIVED_DISPOSITION = "duplicate"


def owed_review_issued(hypothesis: Hypothesis) -> bool:
    return bool(hypothesis.enrichments.get(OWED_REVIEW_MARKER))


def mark_owed_review_issued(hypothesis: Hypothesis) -> None:
    """A failed call spent the override too; success-only marking would
    reopen it each cycle."""
    hypothesis.enrichments[OWED_REVIEW_MARKER] = True


def _is_owed(hypothesis: Hypothesis) -> bool:
    if hypothesis.review_disposition == _ARCHIVED_DISPOSITION:
        return False
    return not has_peer_review(hypothesis)


def owed_review_targets(hypotheses: Iterable[Hypothesis]) -> list[Hypothesis]:
    issued = 0
    pending: list[Hypothesis] = []
    for hypothesis in hypotheses:
        if owed_review_issued(hypothesis):
            issued += 1
        elif _is_owed(hypothesis):
            pending.append(hypothesis)
    return pending[: max(MAX_OWED_REVIEW_OVERRIDES_PER_RUN - issued, 0)]


def owed_review_count(hypotheses: Iterable[Hypothesis]) -> int:
    return len(owed_review_targets(hypotheses))
