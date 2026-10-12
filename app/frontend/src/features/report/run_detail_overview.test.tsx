import {screen} from '@testing-library/react';
import {expect, it, describe} from 'vitest';
import type {Hypothesis} from '@/shared/api/runs';
import {makeHypothesis, makeMatch} from '@/shared/testing/fixtures';
import {
  renderFullReport,
  renderOverview,
} from './run_detail_overview_test_support';

describe('run detail overview', () => {
  it('falls back to live data when there is no persisted report', async () => {
    const hypotheses: Hypothesis[] = [
      makeHypothesis({id: 'h1', title: 'Top idea', elo_rating: 1700}),
    ];
    renderOverview({hypotheses, matches: [makeMatch(1)]});

    expect(
      screen.getByText(
        'The research overview appears after Co-Scientist finishes the final ' +
          'synthesis step.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('Top idea')).toBeInTheDocument();
    expect(
      screen.getByText('1 tournament matches have been recorded for this run.'),
    ).toBeInTheDocument();
  });

  it('keeps server publication order in the live fallback', () => {
    renderOverview({
      hypotheses: [
        makeHypothesis({
          id: 'played',
          title: 'Played first',
          elo_rating: 1184,
          win_count: 1,
        }),
        makeHypothesis({
          id: 'unplayed',
          title: 'Unplayed second',
          elo_rating: 1200,
        }),
      ],
    });
    const played = screen.getByText('Played first');
    const unplayed = screen.getByText('Unplayed second');
    expect(
      played.compareDocumentPosition(unplayed) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('renders the synthesized summary and idea buckets from the report', () => {
    renderFullReport();

    expect(
      screen.getByText('A synthesized summary of the research.'),
    ).toBeInTheDocument();
    expect(screen.getByText('High Potential')).toBeInTheDocument();
    expect(screen.getByText('Non-Viable')).toBeInTheDocument();
    expect(screen.getByText('Verified ideas')).toBeInTheDocument();
    expect(screen.getByText('Sources Analyzed')).toBeInTheDocument();
    expect(screen.getByText('11')).toBeInTheDocument();
  });
});
