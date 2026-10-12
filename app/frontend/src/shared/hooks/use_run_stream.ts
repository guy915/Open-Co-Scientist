import {type Dispatch, type SetStateAction, useEffect, useState} from 'react';
import {
  API_BASE_URL,
  clientHeaders,
  fetchWithSession,
  readSseFrames,
} from '@/shared/api/runs';

export interface StreamEvent {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at?: number;
}

// Permanent auth/not-found rejection disconnects without retries.
export type StreamConnectionState =
  'connecting' | 'open' | 'reconnecting' | 'disconnected' | 'capacity';

export interface UseRunStreamResult {
  events: StreamEvent[];
  terminal: boolean;
  connection: StreamConnectionState;
}

export const RECONNECT_DELAY_MS = 2000;
const MAX_RECONNECT_DELAY_MS = 30_000;

// Back off while the server keeps failing, so an outage is not met with a
// request every two seconds from every open run page.
export function reconnectDelayMs(failures: number): number {
  return Math.min(
    RECONNECT_DELAY_MS * 2 ** Math.max(0, failures - 1),
    MAX_RECONNECT_DELAY_MS,
  );
}

// Batch replay bursts and deduplicate reconnects by sequence.
function createEventBatcher(
  setEvents: Dispatch<SetStateAction<StreamEvent[]>>,
) {
  let buffer: StreamEvent[] = [];
  let flushTimer = 0;
  const seen = new Set<number>();

  const flush = () => {
    flushTimer = 0;
    if (buffer.length === 0) return;
    const batch = buffer;
    buffer = [];
    setEvents(prev => [...prev, ...batch]);
  };

  return {
    push(ev: StreamEvent) {
      if (seen.has(ev.seq)) return;
      seen.add(ev.seq);
      buffer.push(ev);
      if (flushTimer === 0) flushTimer = window.setTimeout(flush, 0);
    },
    flushNow() {
      window.clearTimeout(flushTimer);
      flush();
    },
    cancelPending() {
      window.clearTimeout(flushTimer);
    },
  };
}

// Replay from zero on reconnect to hydrate the complete timeline.
export function useRunStream(runId: string | null): UseRunStreamResult {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [terminal, setTerminal] = useState(false);
  const [connection, setConnection] =
    useState<StreamConnectionState>('connecting');

  useEffect(() => {
    if (!runId) return;
    setEvents([]);
    setTerminal(false);
    setConnection('connecting');

    const batcher = createEventBatcher(setEvents);
    let cancelled = false;
    let opened = false;
    let controller: AbortController | null = null;
    let retryTimer = 0;
    let failures = 0;

    async function connect(): Promise<void> {
      while (!cancelled) {
        controller = new AbortController();
        let capacityDelay: number | null = null;
        try {
          const res = await fetchWithSession(
            `${API_BASE_URL}/api/runs/${runId}/events`,
            {headers: clientHeaders(), signal: controller.signal},
          );
          if (cancelled) return;
          if (res.status === 429) {
            const seconds = Number(res.headers.get('Retry-After'));
            capacityDelay =
              Number.isFinite(seconds) && seconds > 0
                ? Math.min(300, Math.max(30, seconds)) * 1000
                : 30_000;
          }
          if (!res.ok || !res.body) {
            if ([401, 403, 404].includes(res.status)) {
              setConnection('disconnected');
              return;
            }
            throw new Error('Events stream unavailable');
          }
          opened = true;
          failures = 0;
          setConnection('open');
          for await (const ev of readSseFrames<StreamEvent>(res)) {
            if (cancelled) return;
            if (ev.type === '_terminal') {
              batcher.flushNow();
              setTerminal(true);
              return;
            }
            batcher.push(ev);
          }
        } catch {
          // Dropped/malformed streams reconnect; sequence deduplication makes
          // replay safe.
        } finally {
          controller.abort();
        }
        if (cancelled) return;
        setConnection(
          capacityDelay !== null
            ? 'capacity'
            : opened
              ? 'reconnecting'
              : 'connecting',
        );
        await new Promise<void>(resolve => {
          failures += 1;
          retryTimer = window.setTimeout(
            resolve,
            capacityDelay ?? reconnectDelayMs(failures),
          );
        });
      }
    }

    void connect();
    return () => {
      cancelled = true;
      batcher.cancelPending();
      controller?.abort();
      window.clearTimeout(retryTimer);
    };
  }, [runId]);
  return {events, terminal, connection};
}
