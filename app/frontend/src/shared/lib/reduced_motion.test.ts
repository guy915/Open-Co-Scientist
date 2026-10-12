import {afterEach, expect, it, vi} from 'vitest';
import {prefersReducedMotion, scrollBehavior} from './reduced_motion';

afterEach(() => vi.unstubAllGlobals());

function stubMotion(reduce: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockReturnValue({
      matches: reduce,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
}

it('scrolls without gliding when motion is reduced', () => {
  stubMotion(true);
  expect(prefersReducedMotion()).toBe(true);
  expect(scrollBehavior()).toBe('auto');
});

it('glides otherwise', () => {
  stubMotion(false);
  expect(scrollBehavior()).toBe('smooth');
});
