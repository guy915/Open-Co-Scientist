import {act, renderHook} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {EXIT_MS, usePresence} from './use_presence';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

it('mounts at once and unmounts only after the exit', () => {
  const {result, rerender} = renderHook(({open}) => usePresence(open), {
    initialProps: {open: false},
  });
  expect(result.current.mounted).toBe(false);
  rerender({open: true});
  expect(result.current).toEqual({mounted: true, state: 'open'});
  rerender({open: false});
  expect(result.current).toEqual({mounted: true, state: 'closed'});
  act(() => {
    vi.advanceTimersByTime(EXIT_MS);
  });
  expect(result.current.mounted).toBe(false);
});

it('reopening during the exit keeps the element', () => {
  const {result, rerender} = renderHook(({open}) => usePresence(open), {
    initialProps: {open: true},
  });
  rerender({open: false});
  rerender({open: true});
  act(() => {
    vi.advanceTimersByTime(EXIT_MS * 2);
  });
  expect(result.current).toEqual({mounted: true, state: 'open'});
});
