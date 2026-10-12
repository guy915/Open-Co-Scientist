import {expect, it} from 'vitest';

import {makeHypothesis} from '@/shared/testing/fixtures';

import {presentedHypotheses, ratingLabel} from './hypotheses';

it('preserves the server publication order when filtering withdrawn ideas', () => {
  const hypotheses = [
    makeHypothesis({id: 'played', elo_rating: 1184, win_count: 1}),
    makeHypothesis({id: 'unplayed', elo_rating: 1200}),
    makeHypothesis({id: 'withdrawn', status: 'rejected'}),
    makeHypothesis({
      id: 'undermined',
      elo_rating: 1400,
      win_count: 3,
      verification_verdict: 'undermined',
    }),
  ];
  expect(presentedHypotheses(hypotheses).map(h => h.id)).toEqual([
    'played',
    'unplayed',
    'undermined',
  ]);
});

it('drops the ideas a run withdrew from the presented list', () => {
  const kept = makeHypothesis({id: 'kept'});
  const presented = presentedHypotheses([
    kept,
    makeHypothesis({id: 'a', status: 'rejected'}),
    makeHypothesis({id: 'b', status: 'duplicate'}),
  ]);

  expect(presented).toEqual([kept]);
});

it('says "Unranked" for an unplayed idea still in good standing', () => {
  // Final-wave ideas can arrive after the last comparisons.
  expect(ratingLabel(makeHypothesis({status: 'active'}))).toBe('Unranked');
  expect(ratingLabel(makeHypothesis({status: null}))).toBe('Unranked');
});
