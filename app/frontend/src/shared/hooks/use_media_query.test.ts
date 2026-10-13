import {act, renderHook} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {useMediaQuery} from './use_media_query';

afterEach(() => vi.unstubAllGlobals());

function stubQuery(initial: boolean) {
  const listeners = new Set<() => void>();
  const media = {
    matches: initial,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
  };
  vi.stubGlobal('matchMedia', () => media);
  return (matches: boolean) => {
    media.matches = matches;
    listeners.forEach(cb => cb());
  };
}

it('reads the live query on first render and follows changes', () => {
  const change = stubQuery(true);
  const {result} = renderHook(() => useMediaQuery('(max-width: 700px)'));
  expect(result.current).toBe(true);
  act(() => change(false));
  expect(result.current).toBe(false);
});

it('uses the fallback without matchMedia', () => {
  vi.stubGlobal('matchMedia', undefined);
  const {result} = renderHook(() => useMediaQuery('(x)', true));
  expect(result.current).toBe(true);
});
