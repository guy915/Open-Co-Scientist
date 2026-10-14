import {act, render, screen} from '@testing-library/react';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {DeferredHomeLanding} from './deferred_home_landing';

vi.mock('./home_landing', () => ({
  default: () => <div>Landing content</div>,
}));

let notify: IntersectionObserverCallback;
const disconnect = vi.fn();
const observe = vi.fn();

beforeEach(() => {
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(callback: IntersectionObserverCallback) {
        notify = callback;
      }
      observe = observe;
      disconnect = disconnect;
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/');
});

describe('DeferredHomeLanding', () => {
  it('retains the home layout marker until the landing enters view', async () => {
    const {container} = render(<DeferredHomeLanding />);
    expect(container.querySelector('#landing.ucs-landing')).not.toBeNull();
    expect(screen.queryByText('Landing content')).toBeNull();
    act(() =>
      notify(
        [
          {isIntersecting: true, intersectionRatio: 0},
        ] as IntersectionObserverEntry[],
        {} as IntersectionObserver,
      ),
    );
    expect(screen.queryByText('Landing content')).toBeNull();
    act(() =>
      notify(
        [
          {isIntersecting: true, intersectionRatio: 1},
        ] as IntersectionObserverEntry[],
        {} as IntersectionObserver,
      ),
    );
    expect(await screen.findByText('Landing content')).toBeVisible();
    expect(disconnect).toHaveBeenCalled();
  });

  it('loads direct section links without requiring a scroll', async () => {
    window.history.replaceState(null, '', '/#faq');
    render(<DeferredHomeLanding />);
    expect(await screen.findByText('Landing content')).toBeVisible();
  });

  it('loads when intersection observation is unavailable', async () => {
    vi.stubGlobal('IntersectionObserver', undefined);
    render(<DeferredHomeLanding />);
    expect(await screen.findByText('Landing content')).toBeVisible();
  });
});
