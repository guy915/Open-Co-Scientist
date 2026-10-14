import {resetRunDetailMocks} from './run_detail_api_test_support';
import * as runsApi from '@/shared/api/runs';
import {type Evidence, type RunWithSummary} from '@/shared/api/runs';
import {screen} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {makeRunWithSetup} from '@/shared/testing/fixtures';
import {renderAt} from './run_detail_test_support';

beforeEach(() => {
  resetRunDetailMocks();
  vi.mocked(runsApi.listInterviews).mockResolvedValue([]);
});

it.each([
  ['failed', 'Run failed', 'engine.node.generate exhausted its retry budget'],
  ['cancelled', 'Run cancelled', undefined],
] as const)(
  'renders the %s end state with its recorded error, not report tabs',
  async (status, heading, error) => {
    vi.mocked(runsApi.getRun).mockResolvedValue({
      ...makeRunWithSetup('Study pathway X'),
      status,
      error: error ?? null,
    });

    renderAt('/runs/run-1/details');

    expect(await screen.findByText(heading)).toBeInTheDocument();
    if (error) expect(screen.getByText(error)).toBeInTheDocument();
    expect(screen.queryByRole('link', {name: 'Goal Details'})).toBeNull();
    expect(screen.queryByRole('link', {name: 'All Ideas'})).toBeNull();
    expect(screen.queryByText('Run Specifications')).toBeNull();
  },
);

function failedRun(failureKind?: string) {
  return {
    ...makeRunWithSetup('Study pathway X'),
    status: 'failed' as const,
    failure_kind: failureKind,
    error: 'LLM call budget exhausted after 2500 requests',
  };
}

it('shows exact call-budget guidance with an accessible label and keeps the recorded error', async () => {
  vi.mocked(runsApi.getRun).mockResolvedValue(
    failedRun('llm_call_budget_exceeded'),
  );

  renderAt('/runs/run-1/details');

  const guidance = await screen.findByRole('region', {
    name: 'Suggested next step',
  });
  expect(guidance).toHaveTextContent(
    'The run reached its configured model-call limit before it completed. Start a new run with a narrower research goal.',
  );
  expect(screen.getByText('Recorded error')).toBeInTheDocument();
  expect(
    screen.getByText('LLM call budget exhausted after 2500 requests'),
  ).toBeInTheDocument();
});

it('does not show provider guidance when a run is blocked', async () => {
  vi.mocked(runsApi.getRun).mockResolvedValue({
    ...makeRunWithSetup('Study pathway X'),
    status: 'blocked',
    failure_kind: 'llm_timeout',
    error: 'Safety or cancellation detail',
  });

  renderAt('/runs/run-1/details');

  expect(await screen.findByText('Run blocked')).toBeInTheDocument();
  expect(
    screen.queryByRole('region', {name: 'Suggested next step'}),
  ).not.toBeInTheDocument();
  expect(screen.getByText('Safety or cancellation detail')).toBeInTheDocument();
});

it('omits the notice for a paused run with nothing left to review', async () => {
  vi.mocked(runsApi.getRun).mockResolvedValue({
    ...makeRunWithSetup('Study pathway X'),
    status: 'paused',
    awaiting_decision_count: 0,
  });

  renderAt('/runs/run-1/details');

  await screen.findByText('Run Specifications');
  expect(screen.queryByRole('note')).toBeNull();
});

it.each(['paused', 'failed'] as const)(
  'offers acknowledged recovery for a resumable %s run and hides partial reports',
  async status => {
    vi.mocked(runsApi.getRun).mockResolvedValue({
      ...makeRunWithSetup('Study pathway X'),
      status,
      can_resume: true,
    });
    renderAt('/runs/run-1/details');
    expect(
      await screen.findByRole('button', {name: 'Resume run'}),
    ).toBeInTheDocument();
    expect(screen.getByText('Run interrupted')).toBeInTheDocument();
    expect(screen.queryByText('Run Specifications')).toBeNull();
    expect(screen.queryByRole('link', {name: 'All Ideas'})).toBeNull();
  },
);

const UNGROUNDED_NOTICE = /No literature was retrieved for this run/;

const EVIDENCE_ROW = {
  id: 'ev-1',
  title: 'Retrieved paper',
  source: 'pubmed',
  url: 'https://example.org/paper',
  authors: [],
  year: 2024,
  available: true,
  retracted: false,
} as Evidence;

it('flags a completed run with no retrieved evidence as ungrounded', async () => {
  vi.mocked(runsApi.getRun).mockResolvedValue({
    ...makeRunWithSetup('Study pathway X'),
    provider: 'engine',
    llm_backend: 'real',
  } as RunWithSummary);
  vi.mocked(runsApi.getEvidence).mockResolvedValue([]);

  renderAt('/runs/run-1/details');

  expect(await screen.findByText(UNGROUNDED_NOTICE)).toBeInTheDocument();
});

it.each([
  ['the run retrieved evidence', 'engine', 'real', [EVIDENCE_ROW]],
  ['it is offline-backed', 'engine', 'offline', []],
])(
  'omits the ungrounded notice when %s',
  async (_name, provider, llm_backend, evidence) => {
    vi.mocked(runsApi.getRun).mockResolvedValue({
      ...makeRunWithSetup('Study pathway X'),
      provider,
      llm_backend,
    } as RunWithSummary);
    vi.mocked(runsApi.getEvidence).mockResolvedValue(evidence);

    renderAt('/runs/run-1/details');

    await screen.findByText('Run Specifications');
    expect(screen.queryByText(UNGROUNDED_NOTICE)).toBeNull();
  },
);

it('shows only the error for a run that never loaded, without report tabs or an empty report', async () => {
  vi.mocked(runsApi.getRun).mockRejectedValue(new Error('run not found'));

  renderAt('/runs/missing-run/details');

  expect(await screen.findByRole('alert')).toHaveTextContent('run not found');
  expect(screen.queryByRole('link', {name: 'Goal Details'})).toBeNull();
  expect(screen.queryByText('Run Specifications')).toBeNull();
  expect(screen.queryByText('Loading…')).toBeNull();
});
