import {afterEach, expect, it, vi} from 'vitest';
import {fetchWithSession} from './runs';
import {DIAGNOSTIC_EVENT} from '@/shared/lib/diagnostic_events';

afterEach(() => vi.unstubAllGlobals());

function observe() {
  const details: unknown[] = [];
  const listener = (event: Event) =>
    details.push((event as CustomEvent).detail);
  window.addEventListener(DIAGNOSTIC_EVENT, listener);
  return {
    details,
    stop: () => window.removeEventListener(DIAGNOSTIC_EVENT, listener),
  };
}

it('records failed HTTP status and path without query, headers or body', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response('', {status: 403})),
  );
  const observed = observe();
  await fetchWithSession('/api/runs/private-run?token=secret', {
    method: 'POST',
    headers: {Authorization: 'Bearer secret'},
    body: 'secret',
  });
  observed.stop();
  expect(observed.details).toEqual([
    {
      stage: 'fetch_failed',
      level: 'warning',
      payload: {method: 'POST', path: '/api/runs/private-run', status: 403},
    },
  ]);
  expect(JSON.stringify(observed.details)).not.toContain('secret');
});

it('preserves network failures while excluding their potentially private error text', async () => {
  const error = new Error('private credential in failed transport');
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(error));
  const observed = observe();
  await expect(fetchWithSession('/api/runs')).rejects.toBe(error);
  observed.stop();
  expect(observed.details).toEqual([
    {
      stage: 'fetch_failed',
      level: 'warning',
      payload: {method: 'GET', path: '/api/runs', status: 'network'},
    },
  ]);
});

it('never recursively logs a failed log ingestion request', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response('', {status: 503})),
  );
  const observed = observe();
  await fetchWithSession('/api/logs', {method: 'POST'});
  observed.stop();
  expect(observed.details).toEqual([]);
});
