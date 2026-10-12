import {beforeEach, describe, expect, it, vi} from 'vitest';
import {
  setStoredApiKey,
  setStoredApiProvider,
  setStoredModel,
} from '@/shared/lib/client_id';
import {
  getPendingCreateIntent,
  readPendingCreateIntent,
} from './chat_session_start_run';

const setClientId = (id: string) =>
  localStorage.setItem('co_scientist_client_id', id);

const PAYLOAD = {
  research_goal: 'map the pathway',
  interview_id: 'chat-1',
  requirements: ['human cells'],
  document_ids: ['doc-1'],
};

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

describe('pending create intent', () => {
  it('recovers the same intent after reloading with the saved BYOK key', async () => {
    setClientId('client-one');
    setStoredApiProvider('openai');
    setStoredApiKey('sk-secret-one');
    const first = await getPendingCreateIntent('chat-1', PAYLOAD);
    const storedRecord = sessionStorage.getItem(
      'co_scientist_pending_run_create:chat-1',
    );

    vi.resetModules();
    const afterReload = await import('./chat_session_start_run');
    const credentials = await import('@/shared/lib/client_id');
    expect(credentials.getStoredApiKey('openai')).toBe('sk-secret-one');
    expect((await afterReload.readPendingCreateIntent('chat-1'))?.key).toBe(
      first.key,
    );
    const second = await afterReload.getPendingCreateIntent('chat-1', PAYLOAD);

    expect(second.key).toBe(first.key);
    expect(JSON.stringify(second.payload)).toBe(JSON.stringify(PAYLOAD));
    expect(storedRecord).not.toContain('sk-secret-one');
    credentials.clearStoredApiKeys();
  });

  it('does not expose an intent after the owner changes', async () => {
    setClientId('client-one');
    await getPendingCreateIntent('chat-1', PAYLOAD);
    setClientId('client-two');

    expect(await readPendingCreateIntent('chat-1')).toBeUndefined();
  });

  it.each([
    [
      'the owner changes',
      () => setClientId('client-one'),
      () => setClientId('client-two'),
      PAYLOAD,
    ],
    [
      'the create payload changes',
      () => undefined,
      () => undefined,
      {...PAYLOAD, research_goal: 'a different goal'},
    ],
    [
      'the explicit BYOK credential changes',
      () => {
        setStoredApiProvider('openai');
        setStoredApiKey('sk-secret-one');
      },
      () => setStoredApiKey('sk-secret-two'),
      PAYLOAD,
    ],
    [
      'the supervisor credential changes',
      () => {
        setStoredApiProvider('openai');
        setStoredApiKey('sk-worker');
        setStoredApiKey('sk-supervisor-one', 'gemini');
        setStoredModel('supervisor', {
          provider: 'gemini',
          model: 'gemini/gemini-3.8-flash',
        });
      },
      () => setStoredApiKey('sk-supervisor-two', 'gemini'),
      PAYLOAD,
    ],
  ])('rotates the key when %s', async (_name, before, change, payload) => {
    before();
    const first = await getPendingCreateIntent('chat-1', PAYLOAD);
    change();

    const second = await getPendingCreateIntent('chat-1', payload);

    expect(second.key).not.toBe(first.key);
    expect(
      sessionStorage.getItem('co_scientist_pending_run_create:chat-1'),
    ).not.toMatch(/secret|sk-/);
  });
});
