import type {ReactElement} from 'react';
import {it, expect} from 'vitest';
import {render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {IdeasTab} from './ideas_tab';
import type {Hypothesis} from '@/shared/api/runs';
import {makeHypothesis} from '@/shared/testing/fixtures';

function renderIdeas(ui: ReactElement, path = '/runs/run-1/ideas') {
  return render(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>);
}

it('renders idea rows in server publication order with their scores', () => {
  const hypotheses: Hypothesis[] = [
    makeHypothesis({
      id: 'high',
      title: 'High-ranked idea',
      statement: 'A stronger statement.',
      elo_rating: 1300,
      win_count: 4,
      loss_count: 1,
    }),
    makeHypothesis({
      id: 'low',
      title: 'Low-ranked idea',
      statement: 'A weaker statement.',
      elo_rating: 1150,
    }),
  ];
  renderIdeas(<IdeasTab hypotheses={hypotheses} reviews={[]} />);
  expect(screen.getAllByText('High-ranked idea')[0]).toBeInTheDocument();
  expect(screen.getByText('Low-ranked idea')).toBeInTheDocument();
  expect(screen.getByText('Elo rating: 1300')).toBeInTheDocument();
  expect(screen.queryByText('80% wins')).not.toBeInTheDocument();

  const rows = screen.getAllByRole('listitem');
  expect(rows[0]).toHaveTextContent('High-ranked idea');
  expect(rows[1]).toHaveTextContent('Low-ranked idea');
});

it('flags an evidence-less idea Unverified and a probe-falsified one Undermined, showing only the stronger caution when both', () => {
  // Contradictory evidence and absent support are different findings; two
  // chips would wrap the row, so the stronger caution wins.
  const {container} = renderIdeas(
    <IdeasTab
      hypotheses={[
        makeHypothesis({
          id: 'grounded',
          title: 'Grounded idea',
          elo_rating: 1300,
        }),
        makeHypothesis({
          id: 'latent',
          title: 'Latent idea',
          elo_rating: 1250,
          unverified: true,
        }),
        makeHypothesis({
          id: 'doubted',
          title: 'Doubted idea',
          elo_rating: 1400,
          verification_verdict: 'undermined',
        }),
        makeHypothesis({
          id: 'both',
          title: 'Doubly flagged idea',
          elo_rating: 1165,
          unverified: true,
          verification_verdict: 'undermined',
        }),
      ]}
      reviews={[]}
    />,
  );

  const chips = (selector: string) =>
    [...container.querySelectorAll(selector)].map(
      chip => chip.closest('a')?.textContent,
    );
  expect(chips('.idea-unverified-chip')).toEqual([
    expect.stringContaining('Latent idea'),
  ]);
  expect(chips('.idea-undermined-chip')).toEqual([
    expect.stringContaining('Doubted idea'),
    expect.stringContaining('Doubly flagged idea'),
  ]);
  expect(
    container.querySelectorAll('.idea-rank-head .idea-unverified-chip'),
  ).toHaveLength(1);
});

it('marks a screened idea Screened instead of Unverified', () => {
  const {container} = renderIdeas(
    <IdeasTab
      hypotheses={[
        makeHypothesis({
          id: 'finalist',
          title: 'Finalist idea',
          elo_rating: 1300,
          verification_verdict: 'holds',
          screened: false,
        }),
        makeHypothesis({
          id: 'screened',
          title: 'Screened idea',
          elo_rating: 1250,
          screened: true,
        }),
      ]}
      reviews={[]}
    />,
  );

  const chips = [...container.querySelectorAll('.idea-screened-chip')];
  expect(chips.map(chip => chip.closest('a')?.textContent)).toEqual([
    expect.stringContaining('Screened idea'),
  ]);
  expect(chips[0]).toHaveAttribute(
    'aria-label',
    expect.stringContaining('Screened, not deep-verified'),
  );
  expect(container.querySelectorAll('.idea-unverified-chip')).toHaveLength(0);
});
