from __future__ import annotations

import dataclasses
import hashlib
import random

from co_scientist.core.constants import TOURNAMENT_MIN_MATCHES_PER_HYPOTHESIS
from co_scientist.domains.research_state.models import Hypothesis


def _build_match_candidates(
    hypotheses: list[Hypothesis],
) -> list[MatchCandidate]:
    return [
        MatchCandidate(
            id=h.id,
            elo=h.elo_rating,
            matches=h.total_matches,
            cluster_id=h.similarity_cluster_id,
        )
        for h in hypotheses
    ]


def build_tournament_pairings(
    hypotheses: list[Hypothesis],
    tournament_rounds: int,
    research_goal: str,
    current_iteration: int,
    judged: set[frozenset[str]] | None = None,
) -> list[tuple[Hypothesis, Hypothesis]]:
    """Use a process-stable hash seed so repeated inputs retain pairing
    identity."""
    seed_string = f"{research_goal}_{current_iteration}"
    seed = int(hashlib.md5(seed_string.encode()).hexdigest()[:8], 16)

    by_id = {h.id: h for h in hypotheses}
    candidates = _build_match_candidates(hypotheses)
    id_pairs = build_weighted_pairings(candidates, tournament_rounds, seed, exclude=judged)
    return [(by_id[a], by_id[b]) for a, b in id_pairs]


@dataclasses.dataclass(frozen=True)
class MatchCandidate:
    id: str
    elo: int
    matches: int
    cluster_id: str | None = None


@dataclasses.dataclass
class MatchmakingWeights:
    """Matcher and budget share one coverage floor; independent floors leave
    ideas at one match and its two possible ratings."""

    recency: float = 1.0
    rank: float = 1.0
    coverage: float = 0.5
    elo_closeness: float = 2.0
    similarity_bonus: float = 2.0
    min_coverage: int = TOURNAMENT_MIN_MATCHES_PER_HYPOTHESIS


@dataclasses.dataclass(frozen=True)
class _PairingState:
    coverage: dict[str, int]
    weights: MatchmakingWeights
    rng: random.Random
    elo_lo: int
    elo_hi: int


def _elo_range(candidates: list[MatchCandidate]) -> tuple[int, int]:
    elos = [c.elo for c in candidates]
    return min(elos), max(elos)


def _priority(candidate: MatchCandidate, state: _PairingState) -> float:
    recency = 1.0 / (1.0 + candidate.matches)
    span = (state.elo_hi - state.elo_lo) or 1
    rank = (candidate.elo - state.elo_lo) / span
    played = state.coverage.get(candidate.id, 0)
    low_coverage = 1.0 / (1.0 + played)
    return (
        state.weights.recency * recency
        + state.weights.rank * rank
        + state.weights.coverage * low_coverage
    )


def _weighted_choice(
    scored: list[tuple[MatchCandidate, float]], rng: random.Random
) -> MatchCandidate:
    total = sum(score for _, score in scored)
    if total <= 0:
        return rng.choice([c for c, _ in scored])
    threshold = rng.random() * total
    cumulative = 0.0
    for candidate, score in scored:
        cumulative += score
        if cumulative >= threshold:
            return candidate
    return scored[-1][0]


def _coverage_pool(
    candidates: list[MatchCandidate],
    state: _PairingState,
) -> list[MatchCandidate]:
    """Fill the fewest-played level first; second matches must not crowd out
    firsts when enough rounds exist to cover everyone."""
    below = [c for c in candidates if state.coverage.get(c.id, 0) < state.weights.min_coverage]
    if not below:
        return candidates
    fewest = min(state.coverage.get(c.id, 0) for c in below)
    return [c for c in below if state.coverage.get(c.id, 0) == fewest]


def _select_primary(
    candidates: list[MatchCandidate],
    state: _PairingState,
) -> MatchCandidate:
    scored = [(c, _priority(c, state)) for c in _coverage_pool(candidates, state)]
    return _weighted_choice(scored, state.rng)


def _is_eligible_partner(
    candidate: MatchCandidate,
    primary: MatchCandidate,
    recent_pairs: set[frozenset[str]],
) -> bool:
    if candidate.id == primary.id:
        return False
    return frozenset({primary.id, candidate.id}) not in recent_pairs


def _elo_closeness(
    candidate: MatchCandidate, primary: MatchCandidate, state: _PairingState
) -> float:
    """A tied pool supplies no closeness signal; a flat constant would dilute
    all other partner priorities."""
    span = state.elo_hi - state.elo_lo
    if span == 0:
        return 0.0
    return 1.0 - abs(candidate.elo - primary.elo) / span


def _partner_score(
    candidate: MatchCandidate,
    primary: MatchCandidate,
    state: _PairingState,
) -> float:
    score = _priority(candidate, state) + state.weights.elo_closeness * (
        _elo_closeness(candidate, primary, state)
    )
    same_cluster = primary.cluster_id is not None and candidate.cluster_id == primary.cluster_id
    return score + state.weights.similarity_bonus if same_cluster else score


def _select_partner(
    primary: MatchCandidate,
    candidates: list[MatchCandidate],
    recent_pairs: set[frozenset[str]],
    state: _PairingState,
) -> MatchCandidate | None:
    """Apply coverage to both sides; prioritizing only the primary can spend
    rounds on covered partners while another idea remains unmatched."""
    eligible = [c for c in candidates if _is_eligible_partner(c, primary, recent_pairs)]
    if not eligible:
        return None
    pool = _coverage_pool(eligible, state)
    scored = [(c, _partner_score(c, primary, state)) for c in pool]
    return _weighted_choice(scored, state.rng)


def _select_round_partner(
    primary: MatchCandidate,
    candidates: list[MatchCandidate],
    recent_pairs: set[frozenset[str]],
    prev_pair: frozenset[str] | None,
    state: _PairingState,
) -> MatchCandidate | None:
    """Do not relax into scheduled pairs: this build uses one Elo snapshot,
    so repeats replay evidence instead of refreshing it."""
    prev_only: set[frozenset[str]] = {prev_pair} if prev_pair is not None else set()
    return _select_partner(primary, candidates, recent_pairs | prev_only, state)


def _commit_pairing(
    primary: MatchCandidate,
    partner: MatchCandidate,
    recent_pairs: set[frozenset[str]],
    state: _PairingState,
) -> tuple[str, str]:
    state.coverage[primary.id] += 1
    state.coverage[partner.id] += 1
    recent_pairs.add(frozenset({primary.id, partner.id}))
    return primary.id, partner.id


def _schedule_one_pairing(
    candidates: list[MatchCandidate],
    recent_pairs: set[frozenset[str]],
    prev_pair: frozenset[str] | None,
    state: _PairingState,
) -> tuple[str, str] | None:
    """An exhausted primary must not end the build while other pairs remain."""
    for _ in range(len(candidates)):
        primary = _select_primary(candidates, state)
        partner = _select_round_partner(primary, candidates, recent_pairs, prev_pair, state)
        if partner is not None:
            return _commit_pairing(primary, partner, recent_pairs, state)
    return _any_unscheduled_pairing(candidates, recent_pairs, state)


def _any_unscheduled_pairing(
    candidates: list[MatchCandidate],
    recent_pairs: set[frozenset[str]],
    state: _PairingState,
) -> tuple[str, str] | None:
    """Weighted sampling can miss remaining pairs; deterministic exhaustion
    must describe the pool rather than unlucky draws."""
    for i, primary in enumerate(candidates):
        for partner in candidates[i + 1 :]:
            if frozenset({primary.id, partner.id}) not in recent_pairs:
                return _commit_pairing(primary, partner, recent_pairs, state)
    return None


def _init_pairing_state(
    candidates: list[MatchCandidate],
    seed: int,
    weights: MatchmakingWeights | None,
) -> tuple[_PairingState, int]:
    coverage = {c.id: c.matches for c in candidates}
    elo_lo, elo_hi = _elo_range(candidates)
    state = _PairingState(
        coverage=coverage,
        weights=weights or MatchmakingWeights(),
        rng=random.Random(seed),
        elo_lo=elo_lo,
        elo_hi=elo_hi,
    )
    max_pairs = len(candidates) * (len(candidates) - 1) // 2
    return state, max_pairs


def build_weighted_pairings(
    candidates: list[MatchCandidate],
    rounds: int,
    seed: int,
    weights: MatchmakingWeights | None = None,
    exclude: set[frozenset[str]] | None = None,
) -> list[tuple[str, str]]:
    """Repeated pairs inflate Elo without evidence. Cross-cycle rematches
    remain meaningful after ratings and review context change."""
    if len(candidates) < 2:
        return []
    state, all_pairs = _init_pairing_state(candidates, seed, weights)
    recent_pairs: set[frozenset[str]] = set(exclude or ())
    max_pairs = all_pairs - len(recent_pairs)

    pairings: list[tuple[str, str]] = []
    prev_pair: frozenset[str] | None = None
    for _ in range(min(rounds, max_pairs)):
        pair = _schedule_one_pairing(candidates, recent_pairs, prev_pair, state)
        if pair is None:
            break
        pairings.append(pair)
        prev_pair = frozenset(pair)

    return pairings
