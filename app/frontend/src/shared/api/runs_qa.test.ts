import {
  FakeSseBody,
  fetchMock,
  jsonResponse,
  streamingResponse,
} from '@/shared/api/testing';
import {beforeEach, afterEach, describe, expect, it, vi} from 'vitest';
import {
  askRunQuestion,
  getRunMessages,
  type QaSource,
  announceRunStart,
} from './runs';

describe('runs qa', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const SOURCE: QaSource = {
    n: 1,
    evidence_id: 'ev-1',
    title: 'A supporting paper',
    state: 'verified',
  };

  describe('askRunQuestion', () => {
    it('relays sources and chunks as they stream, then resolves the question id', async () => {
      const body = new FakeSseBody();
      fetchMock().mockResolvedValue(streamingResponse(body));
      const onSources = vi.fn();
      const onChunk = vi.fn();

      const pending = askRunQuestion('run-1', 'Why?', {onSources, onChunk});
      body.push({type: 'sources', sources: [SOURCE]});
      body.push({type: 'chunk', content: 'Because '});
      body.push({type: 'chunk', content: 'evidence.'});
      body.push({type: 'done', question_id: 42});
      body.end();

      await expect(pending).resolves.toBe(42);
      expect(onSources).toHaveBeenCalledWith([SOURCE]);
      expect(onChunk).toHaveBeenNthCalledWith(1, 'Because ');
      expect(onChunk).toHaveBeenNthCalledWith(2, 'evidence.');
    });

    it('throws on an error frame instead of resolving', async () => {
      const body = new FakeSseBody();
      fetchMock().mockResolvedValue(streamingResponse(body));

      const pending = askRunQuestion('run-1', 'Why?');
      body.push({type: 'error', message: 'no model configured'});
      body.end();

      await expect(pending).rejects.toThrow('no model configured');
    });

    it('aborts the underlying fetch when the signal fires', async () => {
      fetchMock().mockImplementation(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () => {
              reject(new DOMException('aborted', 'AbortError'));
            });
          }),
      );
      const controller = new AbortController();

      const pending = askRunQuestion('run-1', 'Why?', {}, controller.signal);
      controller.abort();

      await expect(pending).rejects.toMatchObject({name: 'AbortError'});
    });
  });

  it('unwraps the messages envelope of a run', async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({messages: [{id: 1, content: 'Why?'}]}),
    );

    expect(await getRunMessages('run-1')).toEqual([{id: 1, content: 'Why?'}]);
  });
});

describe('runs start', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('relays reasoning and prose, then resolves what done reports', async () => {
    const body = new FakeSseBody();
    fetchMock().mockResolvedValue(streamingResponse(body));
    const reasoning: string[] = [];
    const chunks: string[] = [];

    const pending = announceRunStart('run-1', 'Start research', {
      onReasoning: fragment => reasoning.push(fragment),
      onChunk: fragment => chunks.push(fragment),
    });
    body.push({type: 'reasoning', content: 'The run exists.'});
    body.push({type: 'chunk', content: 'Your session '});
    body.push({type: 'chunk', content: 'is under way.'});
    body.push({type: 'done', prompt_id: 8, fallback: false});
    body.end();

    expect(await pending).toEqual({fallback: false});
    expect(reasoning).toEqual(['The run exists.']);
    expect(chunks.join('')).toBe('Your session is under way.');
  });
});
