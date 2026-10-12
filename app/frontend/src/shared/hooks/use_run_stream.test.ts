import {
  fetchMock,
  FakeSseBody,
  streamingResponse,
  errorResponse,
} from '@/shared/api/testing';
import {describe, it, expect, vi, beforeEach, afterEach} from 'vitest';
import {renderHook, act} from '@testing-library/react';
import {
  reconnectDelayMs,
  useRunStream,
  RECONNECT_DELAY_MS,
} from './use_run_stream';

// An exhausted response queue hangs to model a connection still in flight.
function queueFetch(...responses: Response[]): void {
  const queue = [...responses];
  fetchMock().mockImplementation(() => {
    const next = queue.shift();
    return next === undefined
      ? new Promise<Response>(() => {})
      : Promise.resolve(next);
  });
}

// Async timer advancement drains the microtask-separated read/decode/flush
// chain.
async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('accumulates streamed events and reports the open state', async () => {
  const body = new FakeSseBody();
  queueFetch(streamingResponse(body));
  const {result} = renderHook(() => useRunStream('run-1'));
  await settle();
  expect(result.current.connection).toBe('open');

  body.push({seq: 1, type: 'node_start', payload: {node: 'generate'}});
  body.push({seq: 2, type: 'node_end', payload: {node: 'generate'}});
  await settle();

  expect(result.current.events).toHaveLength(2);
  expect(result.current.events.map(e => e.type)).toEqual([
    'node_start',
    'node_end',
  ]);
});

it('drops replayed events so a reconnect cannot duplicate the timeline', async () => {
  // Reconnects replay from zero, so duplicate sequence numbers are expected.
  const body = new FakeSseBody();
  queueFetch(streamingResponse(body));
  const {result} = renderHook(() => useRunStream('run-1'));
  await settle();

  body.push({seq: 1, type: 'a', payload: {}});
  body.push({seq: 2, type: 'b', payload: {}});
  body.push({seq: 1, type: 'a', payload: {}});
  body.push({seq: 2, type: 'b', payload: {}});
  body.push({seq: 3, type: 'c', payload: {}});
  await settle();

  expect(result.current.events.map(e => e.seq)).toEqual([1, 2, 3]);
});

describe('terminal events', () => {
  it('marks terminal and stops the stream on a _terminal event', async () => {
    const body = new FakeSseBody();
    queueFetch(streamingResponse(body));
    const {result} = renderHook(() => useRunStream('run-1'));
    await settle();

    body.push({seq: 1, type: 'node_start', payload: {}});
    body.push({type: '_terminal', payload: {}});
    await settle();

    expect(result.current.terminal).toBe(true);
    expect(result.current.events).toHaveLength(1);
    await settle(RECONNECT_DELAY_MS);
    expect(fetchMock().mock.calls).toHaveLength(1);
  });
});

describe('malformed payloads', () => {
  it('does not crash on a malformed frame and reconnects instead', async () => {
    const firstBody = new FakeSseBody();
    const secondBody = new FakeSseBody();
    queueFetch(streamingResponse(firstBody), streamingResponse(secondBody));
    const {result} = renderHook(() => useRunStream('run-1'));
    await settle();

    firstBody.pushRaw('not json{');
    await settle();

    expect(result.current.events).toEqual([]);
    await settle(RECONNECT_DELAY_MS);
    expect(fetchMock().mock.calls.length).toBeGreaterThan(1);
  });
});

it('aborts the in-flight connection on unmount', async () => {
  const body = new FakeSseBody();
  const abortSpy = vi.fn();
  fetchMock().mockImplementation((_url: string, init?: RequestInit) => {
    init?.signal?.addEventListener('abort', abortSpy);
    return Promise.resolve(streamingResponse(body));
  });
  const {unmount} = renderHook(() => useRunStream('run-1'));
  await settle();

  unmount();

  expect(abortSpy).toHaveBeenCalled();
});

describe('connection state', () => {
  it.each([401, 403, 404])('does not retry permanent HTTP %s', async status => {
    queueFetch(errorResponse(status));
    const {result} = renderHook(() => useRunStream('run-1'));
    await settle();

    expect(result.current.connection).toBe('disconnected');
    await settle(RECONNECT_DELAY_MS);
    expect(fetchMock().mock.calls).toHaveLength(1);
  });

  it('retries a transient failure before the stream opens', async () => {
    queueFetch(errorResponse(500), streamingResponse(new FakeSseBody()));
    const {result} = renderHook(() => useRunStream('run-1'));
    await settle();
    expect(result.current.connection).toBe('connecting');

    await settle(RECONNECT_DELAY_MS);
    expect(result.current.connection).toBe('open');
    expect(fetchMock()).toHaveBeenCalledTimes(2);
  });

  it('cancels a pending retry when unmounted', async () => {
    queueFetch(errorResponse(500));
    const {unmount} = renderHook(() => useRunStream('run-1'));
    await settle();
    unmount();

    await settle(RECONNECT_DELAY_MS);
    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  it('reports reconnecting after an open stream drops, then reopens', async () => {
    const firstBody = new FakeSseBody();
    const secondBody = new FakeSseBody();
    queueFetch(streamingResponse(firstBody), streamingResponse(secondBody));
    const {result} = renderHook(() => useRunStream('run-1'));
    await settle();
    expect(result.current.connection).toBe('open');

    firstBody.end();
    await settle();
    expect(result.current.connection).toBe('reconnecting');

    await settle(RECONNECT_DELAY_MS);
    expect(result.current.connection).toBe('open');
  });

  it('backs off while the server keeps failing', async () => {
    queueFetch(
      errorResponse(500),
      errorResponse(500),
      streamingResponse(new FakeSseBody()),
    );
    const {result} = renderHook(() => useRunStream('run-1'));
    await settle();

    await settle(RECONNECT_DELAY_MS);
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    await settle(RECONNECT_DELAY_MS);
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    await settle(RECONNECT_DELAY_MS);
    expect(fetchMock()).toHaveBeenCalledTimes(3);
    expect(result.current.connection).toBe('open');
  });
});

it('caps the reconnect delay', () => {
  expect(reconnectDelayMs(1)).toBe(RECONNECT_DELAY_MS);
  expect(reconnectDelayMs(2)).toBe(2 * RECONNECT_DELAY_MS);
  expect(reconnectDelayMs(20)).toBe(30_000);
});

it('shows capacity refusal and honors a longer retry window', async () => {
  const denied = new Response(
    JSON.stringify({detail: 'Live updates are busy'}),
    {
      status: 429,
      headers: {'Retry-After': '60'},
    },
  );
  queueFetch(denied, streamingResponse(new FakeSseBody()));
  const {result} = renderHook(() => useRunStream('run-1'));
  await settle();
  expect(result.current.connection).toBe('capacity');
  await settle(30_000);
  expect(fetchMock()).toHaveBeenCalledTimes(1);
  await settle(30_000);
  expect(fetchMock()).toHaveBeenCalledTimes(2);
  expect(result.current.connection).toBe('open');
});

it('backs off for thirty seconds when a refusal has no retry header', async () => {
  queueFetch(errorResponse(429), streamingResponse(new FakeSseBody()));
  const {result} = renderHook(() => useRunStream('run-1'));
  await settle();
  expect(result.current.connection).toBe('capacity');
  await settle(RECONNECT_DELAY_MS);
  expect(fetchMock()).toHaveBeenCalledTimes(1);
  await settle(30_000 - RECONNECT_DELAY_MS);
  expect(result.current.connection).toBe('open');
});
