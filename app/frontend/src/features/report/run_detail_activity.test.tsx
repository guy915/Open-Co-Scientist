import {
  resetRunDetailMocks,
  setConnection,
} from './run_detail_api_test_support';
import type {Run} from '@/shared/api/runs';
import * as runsApi from '@/shared/api/runs';
import {screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {Route, Routes, useNavigate} from 'react-router-dom';
import {beforeEach, expect, it, vi} from 'vitest';
import {RunDetail} from './run_detail';
import {renderWithProviders} from '@/shared/testing/render';
import {makeRunWithSetup} from '@/shared/testing/fixtures';

const TAB_NAV = 'Goal report sections';

const pending = <T,>() => new Promise<T>(() => {});

// Run route parameter changes reuse the mounted component; the harness must
// navigate likewise.
function RunSwitcher({to}: {to: string}) {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(to)}>
      switch run
    </button>
  );
}

function renderRunDetail(path: string, switchTo?: string) {
  return renderWithProviders(
    <>
      <Routes>
        <Route path="/runs/:id/:tab" element={<RunDetail />} />
      </Routes>
      {switchTo && <RunSwitcher to={switchTo} />}
    </>,
    {path},
  );
}

const runningRun = (id: string): Run =>
  ({
    ...makeRunWithSetup('Study pathway X'),
    id,
    status: 'running',
  }) as unknown as Run;

it('drops the previous run’s content when the route id changes', async () => {
  renderRunDetail('/runs/run-1/details', '/runs/run-2/details');
  expect(await screen.findByText('Run Specifications')).toBeInTheDocument();

  vi.mocked(runsApi.getRun).mockImplementation(pending);
  await userEvent.click(screen.getByRole('button', {name: 'switch run'}));

  await waitFor(() =>
    expect(screen.queryByText('Run Specifications')).toBeNull(),
  );
  expect(document.querySelector('[aria-busy="true"]')).toBeInTheDocument();
});

beforeEach(() => {
  resetRunDetailMocks();
  setConnection('open');
});

it('shows no report chrome for a run the history reports as running', async () => {
  vi.mocked(runsApi.loadRunHistory).mockResolvedValue([runningRun('run-1')]);
  vi.mocked(runsApi.getRun).mockImplementation(pending);

  renderRunDetail('/runs/run-1/details');

  await waitFor(() => expect(runsApi.loadRunHistory).toHaveBeenCalled());
  expect(screen.queryByRole('navigation', {name: TAB_NAV})).toBeNull();
  expect(document.querySelector('[aria-busy="true"]')).toBeInTheDocument();
});
