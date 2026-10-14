import {screen} from '@testing-library/react';
import {makeRunWithSummary} from '@/shared/testing/fixtures';
import {beforeEach, expect, it, describe} from 'vitest';
import {
  installChatWorkspaceMocks,
  renderWorkspace,
  apiMock,
} from './chat_workspace_test_helpers';

describe('chat workspace home', () => {
  beforeEach(() => {
    installChatWorkspaceMocks();
  });

  it('opens on the reference-style Co-Scientist home screen', async () => {
    renderWorkspace();

    expect(
      screen.getByRole('heading', {
        name: 'What breakthrough should we make today?',
      }),
    ).toBeInTheDocument();
    expect(screen.getByText('Recents')).toBeInTheDocument();
    expect(screen.getByText('Frame the research goal')).toBeInTheDocument();
    expect(screen.getByText('Generate hypotheses')).toBeInTheDocument();
    expect(
      screen.getByText('Pressure-test the best ideas'),
    ).toBeInTheDocument();
    expect(screen.queryByText('AI Co-Scientist')).toBeNull();
    expect(
      screen.getByPlaceholderText('Start a new research goal to begin'),
    ).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeInTheDocument();
    expect(
      await screen.findByText(/ferroptosis in pancreatic cancer cells/i, {
        selector: '.reference-recent-description',
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', {
        name: /ferroptosis in pancreatic cancer cells/i,
      }),
    ).toHaveAttribute('href', '/examples/demo-ferroptosis');
  });
});

describe('chat workspace home completed cards', () => {
  beforeEach(() => {
    installChatWorkspaceMocks();
  });

  it('renders the run’s real top hypotheses on a completed card', async () => {
    apiMock.listDemoRuns.mockResolvedValue([]);
    apiMock.listRuns.mockResolvedValue([
      makeRunWithSummary(
        {
          id: 'run-real-titles',
          research_goal: 'Rank host-pathogen target hypotheses.',
          top_hypotheses: [
            'Metabolic refuge disruption hypothesis',
            'Biofilm redox-state vulnerability hypothesis',
          ],
        },
        'chat',
      ),
    ]);

    renderWorkspace();

    expect(
      await screen.findByText('Metabolic refuge disruption hypothesis'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Biofilm redox-state vulnerability hypothesis'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('link', {name: /Rank host-pathogen target hypotheses/}),
    ).toHaveAttribute('href', '/runs/run-real-titles/details');
  });
});
