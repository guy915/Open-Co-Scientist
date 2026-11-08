import {describe, expect, it} from 'vitest';
import {loadChunk} from './load_chunk';

describe('loadChunk', () => {
  it('passes a loaded module through', async () => {
    const module = {value: 1};
    await expect(loadChunk(async () => module)).resolves.toBe(module);
  });

  it('keeps waiting when a cancelled preload error resolves to undefined', async () => {
    let settled = false;
    void loadChunk(async () => undefined as unknown as {value: number}).then(
      () => (settled = true),
      () => (settled = true),
    );
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(settled).toBe(false);
  });

  it('still rejects a real load failure', async () => {
    const error = new Error('boom');
    await expect(
      loadChunk(async () => {
        throw error;
      }),
    ).rejects.toBe(error);
  });
});
