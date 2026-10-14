import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {RunRecovery} from './run_recovery';
import {resumeRun} from '@/shared/api/runs';
import {makeRunWithSetup} from '@/shared/testing/fixtures';
import {RUNS_CHANGED_EVENT} from '@/shared/lib/dom_events';

vi.mock('@/shared/api/runs', () => ({resumeRun: vi.fn()}));

beforeEach(() => vi.resetAllMocks());

it('requires confirmation before replay and refreshes after admission', async () => {
  vi.mocked(resumeRun).mockResolvedValue({id: 'run-1', status: 'queued'});
  const refreshed = vi.fn();
  const onChanged = vi.fn();
  window.addEventListener(RUNS_CHANGED_EVENT, onChanged);
  render(
    <RunRecovery
      run={makeRunWithSetup('Study pathway X', {
        status: 'failed',
        can_resume: true,
        resume_requires_ack: true,
        failure_kind: 'llm_timeout_unknown',
      })}
      onResumed={refreshed}
    />,
  );
  fireEvent.click(screen.getByRole('button', {name: 'Resume run'}));
  expect(resumeRun).not.toHaveBeenCalled();
  expect(screen.getByRole('dialog')).toHaveTextContent(
    'may repeat provider requests and charges',
  );
  fireEvent.click(screen.getByRole('button', {name: 'Confirm resume'}));
  await waitFor(() => expect(refreshed).toHaveBeenCalledOnce());
  expect(resumeRun).toHaveBeenCalledWith('run-1');
  expect(onChanged).toHaveBeenCalledOnce();
  window.removeEventListener(RUNS_CHANGED_EVENT, onChanged);
});

it('resumes a credit-exhausted run directly with provider-credit guidance', async () => {
  vi.mocked(resumeRun).mockResolvedValue({id: 'run-1', status: 'queued'});
  const refreshed = vi.fn();
  render(
    <RunRecovery
      run={makeRunWithSetup('Study pathway X', {
        status: 'failed',
        can_resume: true,
        resume_requires_ack: false,
        failure_kind: 'llm_credit_exhausted',
      })}
      onResumed={refreshed}
    />,
  );

  expect(screen.getByRole('heading', {name: 'Run paused'})).toBeInTheDocument();
  expect(screen.getByText(/top up your provider account/i)).toBeInTheDocument();
  expect(
    screen.getByText(/rejected request was not accepted/i),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/earlier successful requests and charges/i),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', {name: 'Resume run'}));

  await waitFor(() => expect(refreshed).toHaveBeenCalledOnce());
  expect(resumeRun).toHaveBeenCalledWith('run-1');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});

it('explains that generic paused work is saved', () => {
  render(
    <RunRecovery
      run={makeRunWithSetup('Study pathway X', {
        status: 'paused',
        can_resume: true,
        resume_requires_ack: false,
      })}
      onResumed={vi.fn()}
    />,
  );

  expect(screen.getByRole('heading', {name: 'Run paused'})).toBeInTheDocument();
  expect(screen.getByText(/your completed work is saved/i)).toBeInTheDocument();
});

it('keeps the recovery available when resume admission fails', async () => {
  vi.mocked(resumeRun).mockRejectedValue(
    new Error('concurrent run limit reached'),
  );
  const refreshed = vi.fn();
  render(
    <RunRecovery
      run={makeRunWithSetup('Study pathway X', {
        status: 'failed',
        can_resume: true,
        resume_requires_ack: true,
        failure_kind: 'llm_interrupted_unknown',
      })}
      onResumed={refreshed}
    />,
  );
  fireEvent.click(screen.getByRole('button', {name: 'Resume run'}));
  fireEvent.click(screen.getByRole('button', {name: 'Confirm resume'}));
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'concurrent run limit reached',
  );
  expect(refreshed).not.toHaveBeenCalled();
  expect(screen.getByRole('button', {name: 'Confirm resume'})).toBeEnabled();
});
