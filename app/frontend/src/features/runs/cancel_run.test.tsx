import {act, fireEvent, render, screen} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {HttpError} from '@/shared/api/runs';
import {makeRunWithSummary} from '@/shared/testing/fixtures';
import {RUNS_CHANGED_EVENT} from '@/shared/lib/dom_events';
import {CancelRunControl} from './cancel_run';

const cancelRun = vi.hoisted(() => vi.fn());
const getRun = vi.hoisted(() => vi.fn());
const resumeRun = vi.hoisted(() => vi.fn());

vi.mock('@/shared/api/runs', async () => {
  const actual =
    await vi.importActual<typeof import('@/shared/api/runs')>(
      '@/shared/api/runs',
    );
  return {...actual, cancelRun, getRun, resumeRun};
});

beforeEach(() => {
  cancelRun.mockReset();
  cancelRun.mockResolvedValue({id: 'run-1', status: 'cancelled'});
  getRun.mockReset();
  resumeRun.mockReset();
  resumeRun.mockResolvedValue({id: 'run-1', status: 'queued'});
});

afterEach(() => {
  vi.useRealTimers();
});

function renderControl(status: string | undefined, runId = 'run-1') {
  return render(
    <CancelRunControl
      runId={runId}
      status={status as Parameters<typeof CancelRunControl>[0]['status']}
    />,
  );
}

function stopButton() {
  return screen.getByRole('button', {name: /stop/i});
}

it('offers the stop control for a running run', () => {
  renderControl('running');
  expect(stopButton()).toBeInTheDocument();
});

it('does not offer Stop for a paused run while checking server resume eligibility', async () => {
  getRun.mockResolvedValue(
    makeRunWithSummary({status: 'paused', can_resume: false}),
  );
  renderControl('paused');

  expect(screen.queryByRole('button', {name: /stop/i})).not.toBeInTheDocument();
  expect(
    screen.queryByRole('button', {name: 'Resume run'}),
  ).not.toBeInTheDocument();
  expect(getRun).toHaveBeenCalledWith('run-1');
});

it('offers Resume for a failed run only when the owned run detail allows it', async () => {
  getRun.mockResolvedValue(
    makeRunWithSummary({
      status: 'failed',
      can_resume: true,
      resume_requires_ack: false,
      failure_kind: 'llm_credit_exhausted',
    }),
  );
  renderControl('failed');

  expect(await screen.findByRole('button', {name: 'Resume run'})).toBeEnabled();
  expect(resumeRun).not.toHaveBeenCalled();
});

it('ignores eligibility returned for a previous header run', async () => {
  let resolvePrevious!: (value: ReturnType<typeof makeRunWithSummary>) => void;
  getRun.mockImplementation((id: string) =>
    id === 'run-1'
      ? new Promise(resolve => {
          resolvePrevious = resolve;
        })
      : Promise.resolve(
          makeRunWithSummary({status: 'failed', can_resume: false}),
        ),
  );
  const view = renderControl('failed');

  view.rerender(<CancelRunControl runId="run-2" status="failed" />);
  await act(async () => {
    resolvePrevious(makeRunWithSummary({status: 'failed', can_resume: true}));
  });

  expect(
    screen.queryByRole('button', {name: 'Resume run'}),
  ).not.toBeInTheDocument();
  expect(getRun).toHaveBeenCalledWith('run-1');
  expect(getRun).toHaveBeenCalledWith('run-2');
});

it('asks twice before stopping, since stopping cannot be undone', async () => {
  renderControl('running');

  fireEvent.click(stopButton());

  expect(cancelRun).not.toHaveBeenCalled();
  expect(
    screen.getByRole('button', {name: 'Confirm stop'}),
  ).toBeInTheDocument();

  await act(async () => {
    fireEvent.click(screen.getByRole('button', {name: 'Confirm stop'}));
  });

  expect(cancelRun).toHaveBeenCalledWith('run-1');
});

it('treats "already finished" as the list being stale, not as an error', async () => {
  // A run can finish between polling and cancellation; refreshing removes the
  // stale action.
  cancelRun.mockRejectedValue(new HttpError('run already finished', 409));
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
  const onChanged = vi.fn();
  window.addEventListener(RUNS_CHANGED_EVENT, onChanged);
  renderControl('running');

  fireEvent.click(stopButton());
  await act(async () => {
    fireEvent.click(screen.getByRole('button', {name: 'Confirm stop'}));
  });

  expect(onChanged).toHaveBeenCalled();
  expect(errors).not.toHaveBeenCalled();
  window.removeEventListener(RUNS_CHANGED_EVENT, onChanged);
  errors.mockRestore();
});
