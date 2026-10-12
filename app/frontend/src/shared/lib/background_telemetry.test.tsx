import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, render, waitFor} from '@testing-library/react';
import {StrictMode} from 'react';

const {schedule, init} = vi.hoisted(() => ({schedule: vi.fn(), init: vi.fn()}));
vi.mock('./after_first_view', () => ({afterFirstView: schedule}));
vi.mock('./error_tracking', () => ({initErrorTracking: init}));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

it('schedules once after the view commits, including StrictMode, and loads tracking later', async () => {
  vi.stubEnv('VITE_SENTRY_DSN', 'https://public@127.0.0.1/fp-sentry/1');
  vi.resetModules();
  const {BackgroundTelemetry} = await import('./background_telemetry');
  schedule.mockImplementation(() => {
    expect(document.getElementById('first-view')).toHaveTextContent('Ready');
  });
  render(
    <StrictMode>
      <div id="first-view">Ready</div>
      <BackgroundTelemetry />
    </StrictMode>,
  );
  expect(schedule).toHaveBeenCalledOnce();
  expect(init).not.toHaveBeenCalled();
  schedule.mock.calls[0][0]();
  await waitFor(() =>
    expect(init).toHaveBeenCalledWith('https://public@127.0.0.1/fp-sentry/1'),
  );
});

it('does not schedule tracking without a configured DSN', async () => {
  vi.stubEnv('VITE_SENTRY_DSN', undefined);
  vi.resetModules();
  const {BackgroundTelemetry} = await import('./background_telemetry');
  render(<BackgroundTelemetry />);
  expect(schedule).not.toHaveBeenCalled();
  expect(init).not.toHaveBeenCalled();
});
