import {afterEach, expect, it, vi} from 'vitest';
import {readStorage, removeStorage, writeStorage} from './safe_storage';

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

it('reads back what it wrote', () => {
  expect(writeStorage('local', 'k', 'v')).toBe(true);
  expect(readStorage('local', 'k')).toBe('v');
  expect(removeStorage('local', 'k')).toBe(true);
  expect(readStorage('local', 'k')).toBeNull();
});

it('reports failure instead of throwing when storage is blocked', () => {
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => {
    throw new DOMException('blocked', 'SecurityError');
  });
  expect(readStorage('local', 'k')).toBeNull();
  expect(writeStorage('local', 'k', 'v')).toBe(false);
  expect(removeStorage('local', 'k')).toBe(false);
});

it('reports a full quota as a failed write', () => {
  vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {
    throw new DOMException('full', 'QuotaExceededError');
  });
  expect(writeStorage('local', 'k', 'v')).toBe(false);
});
