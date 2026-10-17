import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, render, waitFor} from '@testing-library/react';
import {StrictMode} from 'react';

const {schedule, init} = vi.hoisted(() => ({schedule: vi.fn(), init: vi.fn()}));
vi.mock('./after_first_view', () => ({afterFirstView: schedule}));
vi.mock('./error_tracking', () => ({initErrorTracking: init}));

afterEach(() => {
  cleanup();
  document.head
    .querySelectorAll('script[data-cf-beacon]')
    .forEach(node => node.remove());
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

it('does not schedule tracking without a configured DSN or analytics token', async () => {
  vi.stubEnv('VITE_SENTRY_DSN', undefined);
  vi.stubEnv('VITE_CF_WEB_ANALYTICS_TOKEN', undefined);
  vi.resetModules();
  const {BackgroundTelemetry} = await import('./background_telemetry');
  render(<BackgroundTelemetry />);
  expect(schedule).not.toHaveBeenCalled();
  expect(init).not.toHaveBeenCalled();
});

it('adds the analytics beacon after the first view without loading error tracking', async () => {
  vi.stubEnv('VITE_SENTRY_DSN', undefined);
  vi.stubEnv('VITE_CF_WEB_ANALYTICS_TOKEN', 'public-site-token');
  vi.resetModules();
  const {BackgroundTelemetry, WEB_ANALYTICS_SRC} =
    await import('./background_telemetry');
  schedule.mockImplementation(() => {});
  render(<BackgroundTelemetry />);
  expect(document.querySelector('script[data-cf-beacon]')).toBeNull();
  schedule.mock.calls[0][0]();
  const script = document.querySelector<HTMLScriptElement>(
    'script[data-cf-beacon]',
  );
  expect(script?.src).toBe(WEB_ANALYTICS_SRC);
  expect(script?.defer).toBe(true);
  expect(JSON.parse(script?.dataset.cfBeacon ?? '')).toEqual({
    token: 'public-site-token',
    spa: true,
  });
  expect(init).not.toHaveBeenCalled();
});
