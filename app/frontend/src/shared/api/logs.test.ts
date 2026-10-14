import {beforeEach, expect, it, vi} from 'vitest';
import {getAppLogs, postAppLogs} from './logs';

const httpMock = vi.hoisted(() => {
  const clientHeaders = vi.fn(() => ({'X-Client-ID': 'client-7'}));
  // Build headers through the identity mock so requests retain their owner.
  const jsonRequest = vi.fn(
    (body: unknown, includeClientId = false): RequestInit => ({
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(includeClientId ? clientHeaders() : {}),
      },
      body: JSON.stringify(body),
    }),
  );
  return {fetchJson: vi.fn(), clientHeaders, jsonRequest};
});

vi.mock('./runs', () => httpMock);

beforeEach(() => {
  httpMock.fetchJson.mockReset();
});

// Unidentified remote log requests match nothing and create unreadable
// ownerless records.
it('identifies the caller when reading', async () => {
  httpMock.fetchJson.mockResolvedValue({logs: [], last_id: 0, total: 0});

  await getAppLogs();

  const [, init] = httpMock.fetchJson.mock.calls[0];
  expect(init.headers).toMatchObject({'X-Client-ID': 'client-7'});
});

it('identifies the caller when posting', async () => {
  httpMock.fetchJson.mockResolvedValue({added: 1, last_id: 5});
  await postAppLogs([{message: 'clicked something'}]);

  const [, init] = httpMock.fetchJson.mock.calls[0];
  expect(init.headers).toMatchObject({'X-Client-ID': 'client-7'});
});

it('asks only for the requested minimum level', async () => {
  httpMock.fetchJson.mockResolvedValue({logs: [], last_id: 0, total: 0});

  await getAppLogs(7, 1, 'WARNING');

  expect(httpMock.fetchJson.mock.calls[0][0]).toBe(
    '/api/logs?after_id=7&limit=1&min_level=WARNING',
  );
});
