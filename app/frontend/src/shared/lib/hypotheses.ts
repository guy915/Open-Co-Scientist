import type {Hypothesis} from '@/shared/api/runs';

// Match the report exclusion set: duplicate/rejected ideas are withdrawn, not
// results.
const WITHDRAWN_STATUSES = new Set(['duplicate', 'rejected']);

// An undermined idea remains visible but demoted; match the engine verdict
// vocabulary.
export const UNDERMINED_VERDICT = 'undermined';

export function presentedHypotheses(hypotheses: Hypothesis[]): Hypothesis[] {
  return hypotheses.filter(
    hypothesis => !WITHDRAWN_STATUSES.has(String(hypothesis.status)),
  );
}

// Only an actual comparison earns a displayed rating; an untouched initial Elo
// must read as unranked.
export function ratingLabel(hypothesis: Hypothesis): string {
  if (hypothesis.win_count + hypothesis.loss_count > 0) {
    return `Elo rating: ${hypothesis.elo_rating}`;
  }
  return 'Unranked';
}
