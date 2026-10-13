import {describe, it, expect, beforeEach, afterEach, vi} from 'vitest';
import {
  getClientId,
  getStoredApiKey,
  keyedProviders,
  makePrefixedId,
  setStoredApiKey,
  clearStoredApiKeys,
  getStoredModel,
  setStoredModel,
} from './client_id';
import {STORAGE_KEYS} from './safe_storage';

describe('client id', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.removeItem('co_scientist_client_id');
  });

  it('uses a secure UUID when available', () => {
    const randomUUID = vi.fn(() => '00000000-0000-4000-8000-000000000001');
    vi.stubGlobal('crypto', {randomUUID});
    expect(makePrefixedId('client')).toBe(
      'client-00000000-0000-4000-8000-000000000001',
    );
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  describe('getClientId', () => {
    beforeEach(() => localStorage.removeItem('co_scientist_client_id'));

    it('generates and persists an id on first use', () => {
      const id = getClientId();
      expect(id).toBeTruthy();
      expect(localStorage.getItem('co_scientist_client_id')).toBe(id);
    });

    it('persists and reuses 128 secure random bits when UUIDs are unavailable', () => {
      const getRandomValues = vi.fn((bytes: Uint8Array) => {
        expect(bytes).toHaveLength(16);
        bytes.set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 255]);
        return bytes;
      });
      vi.stubGlobal('crypto', {getRandomValues});
      const insecureRandom = vi.spyOn(Math, 'random');

      const id = getClientId();
      expect(id).toBe('client-000102030405060708090a0b0c0d0eff');
      expect(localStorage.getItem('co_scientist_client_id')).toBe(id);
      expect(getClientId()).toBe(id);
      expect(getRandomValues).toHaveBeenCalledOnce();
      expect(insecureRandom).not.toHaveBeenCalled();
    });

    it('does not persist an ownership ID without secure randomness', () => {
      vi.stubGlobal('crypto', undefined);
      expect(() => getClientId()).toThrow('Secure randomness is required');
      expect(localStorage.getItem('co_scientist_client_id')).toBeNull();
    });

    it('keeps one id for the page when storage is blocked', () => {
      const blocked = vi
        .spyOn(window, 'localStorage', 'get')
        .mockImplementation(() => {
          throw new DOMException('blocked', 'SecurityError');
        });
      try {
        const first = getClientId();
        expect(first).toMatch(/^client-/);
        expect(getClientId()).toBe(first);
      } finally {
        blocked.mockRestore();
      }
    });
  });
});

describe('api keys', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('keeps one trimmed key per provider', () => {
    setStoredApiKey('  sk-a  ', 'anthropic');
    setStoredApiKey('sk-g', 'gemini');
    expect(getStoredApiKey('anthropic')).toBe('sk-a');
    expect(getStoredApiKey('gemini')).toBe('sk-g');
    expect(getStoredApiKey('openai')).toBe('');
    expect(keyedProviders()).toEqual(['anthropic', 'gemini']);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.apiKeys)!)).toEqual({
      anthropic: 'sk-a',
      gemini: 'sk-g',
    });
    expect(sessionStorage.getItem(STORAGE_KEYS.apiKeys)).toBeNull();
  });

  it('uses page memory when browser storage is blocked', () => {
    const writes = vi.spyOn(localStorage, 'setItem');
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    try {
      setStoredApiKey('  synthetic-key  ', 'openai');
      expect(getStoredApiKey('openai')).toBe('synthetic-key');
      expect(writes).not.toHaveBeenCalled();
    } finally {
      vi.restoreAllMocks();
    }
  });

  it('clears only the removed provider and its model choices', () => {
    setStoredApiKey('worker-key', 'openai');
    setStoredApiKey('supervisor-key', 'gemini');
    setStoredModel('worker', {provider: 'openai', model: 'openai/worker'});
    setStoredModel('supervisor', {
      provider: 'gemini',
      model: 'gemini/supervisor',
    });
    setStoredApiKey('', 'openai');
    expect(keyedProviders()).toEqual(['gemini']);
    expect(getStoredModel('worker')).toBeNull();
    expect(getStoredModel('supervisor')?.provider).toBe('gemini');
  });

  it.each(['current', 'legacy', 'malformed'] as const)(
    'preserves valid %s credentials across page loads and migrates the legacy key',
    async layout => {
      vi.resetModules();
      localStorage.setItem(STORAGE_KEYS.apiProvider, 'openai');
      localStorage.setItem(STORAGE_KEYS.legacyApiKey, ' legacy-key ');
      if (layout !== 'legacy') {
        localStorage.setItem(
          STORAGE_KEYS.apiKeys,
          layout === 'malformed'
            ? '{broken'
            : JSON.stringify({openai: 'current-key', unknown: 'ignored'}),
        );
      }
      const fresh = await import('./client_id');
      const expected =
        layout === 'current'
          ? 'current-key'
          : layout === 'legacy'
            ? 'legacy-key'
            : '';
      expect(fresh.getStoredApiKey('openai')).toBe(expected);
      expect(localStorage.getItem(STORAGE_KEYS.legacyApiKey)).toBeNull();
      vi.resetModules();
      const reloaded = await import('./client_id');
      expect(reloaded.getStoredApiKey('openai')).toBe(expected);
      expect(reloaded.keyedProviders()).toEqual(expected ? ['openai'] : []);
      fresh.clearStoredApiKeys();
    },
  );

  it('reads updates and removals made by another tab', async () => {
    setStoredApiKey('first-key', 'openai');
    vi.resetModules();
    const anotherTab = await import('./client_id');
    expect(anotherTab.getStoredApiKey('openai')).toBe('first-key');
    anotherTab.setStoredApiKey('second-key', 'gemini');
    expect(keyedProviders()).toEqual(['gemini', 'openai']);
    setStoredApiKey('', 'openai');
    expect(anotherTab.getStoredApiKey('openai')).toBe('');
    expect(anotherTab.getStoredApiKey('gemini')).toBe('second-key');
    anotherTab.clearStoredApiKeys();
    expect(keyedProviders()).toEqual([]);
  });

  it('retries failed writes without losing other providers', () => {
    setStoredApiKey('first-key', 'openai');
    const write = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    setStoredApiKey('second-key', 'gemini');
    expect(getStoredApiKey('gemini')).toBe('second-key');
    expect(getStoredApiKey('openai')).toBe('first-key');
    write.mockRestore();
    expect(getStoredApiKey('gemini')).toBe('second-key');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.apiKeys)!)).toEqual({
      openai: 'first-key',
      gemini: 'second-key',
    });
  });

  it('keeps a cleared provider absent when legacy-key removal fails', () => {
    localStorage.setItem(STORAGE_KEYS.apiProvider, 'openai');
    localStorage.setItem(STORAGE_KEYS.legacyApiKey, 'legacy-key');
    const originalRemove = localStorage.removeItem.bind(localStorage);
    const remove = vi
      .spyOn(localStorage, 'removeItem')
      .mockImplementation(key => {
        if (key === STORAGE_KEYS.legacyApiKey) {
          throw new DOMException('blocked', 'SecurityError');
        }
        originalRemove(key);
      });
    expect(getStoredApiKey('openai')).toBe('legacy-key');
    setStoredApiKey('', 'openai');
    expect(getStoredApiKey('openai')).toBe('');
    remove.mockRestore();
    expect(keyedProviders()).toEqual([]);
    expect(localStorage.getItem(STORAGE_KEYS.legacyApiKey)).toBeNull();
  });

  it('does not restore erased credentials after storage removal fails', () => {
    setStoredApiKey('synthetic-key', 'openai');
    localStorage.setItem(STORAGE_KEYS.apiKeys, '{"openai":"old-key"}');
    localStorage.setItem(STORAGE_KEYS.legacyApiKey, 'legacy-key');
    sessionStorage.setItem(STORAGE_KEYS.apiKeys, 'session-key');
    const remove = vi
      .spyOn(localStorage, 'removeItem')
      .mockImplementation(() => {
        throw new DOMException('blocked', 'SecurityError');
      });
    clearStoredApiKeys();
    expect(getStoredApiKey('openai')).toBe('');
    remove.mockRestore();
    expect(keyedProviders()).toEqual([]);
    expect(localStorage.getItem(STORAGE_KEYS.apiKeys)).toBeNull();
    expect(localStorage.getItem(STORAGE_KEYS.legacyApiKey)).toBeNull();
    expect(sessionStorage.getItem(STORAGE_KEYS.apiKeys)).toBeNull();
    expect(keyedProviders()).toEqual([]);
  });
});
