import {act, render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import HomeLanding from './home_landing';

const observers: SectionObserver[] = [];
let settled = false;

class SectionObserver {
  targets = new Set<Element>();
  constructor(private notify: IntersectionObserverCallback) {
    observers.push(this);
  }
  observe(target: Element) {
    this.targets.add(target);
  }
  unobserve(target: Element) {
    this.targets.delete(target);
  }
  disconnect() {
    this.targets.clear();
  }
  reportSafety() {
    const target = document.getElementById('landing-safety')!;
    if (this.targets.has(target)) {
      this.notify(
        [
          {
            target,
            isIntersecting: true,
            time: performance.now(),
            rootBounds: rect(270, 315),
            boundingClientRect: rect(200, 700),
            intersectionRect: rect(270, 315),
            intersectionRatio: 0.09,
          },
        ],
        this as unknown as IntersectionObserver,
      );
    }
  }
}

function rect(top: number, bottom: number): DOMRect {
  return {
    x: 0,
    y: top,
    top,
    bottom,
    left: 0,
    right: 1440,
    width: 1440,
    height: bottom - top,
    toJSON: () => ({}),
  };
}

beforeEach(() => {
  observers.length = 0;
  settled = false;
  vi.stubGlobal('IntersectionObserver', SectionObserver);
  vi.stubGlobal('innerHeight', 900);
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(
    function (this: Element) {
      if (this.id === 'landing-safety') {
        return settled ? rect(-381, 137) : rect(200, 700);
      }
      if (this.id === 'landing-tiers') {
        return settled ? rect(137, 876) : rect(700, 1439);
      }
      return rect(1500, 1600);
    },
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function renderLanding() {
  const {container} = render(
    <MemoryRouter>
      <main className="ucs-page--home">
        <HomeLanding />
      </main>
    </MemoryRouter>,
  );
  act(() => observers.forEach(observer => observer.reportSafety()));
  expect(screen.getByRole('link', {name: 'Safety'})).toHaveAttribute(
    'aria-current',
    'true',
  );
  return container.querySelector('main')!;
}

it('updates the current section when scrolling ends without a final observer entry', () => {
  const pane = renderLanding();
  settled = true;
  act(() => {
    pane.dispatchEvent(new Event('scrollend'));
  });
  expect(screen.getByRole('link', {name: 'Tiers'})).toHaveAttribute(
    'aria-current',
    'true',
  );
});

it('uses current geometry when an earlier observer entry arrives late', () => {
  renderLanding();
  settled = true;
  act(() => observers.forEach(observer => observer.reportSafety()));
  expect(screen.getByRole('link', {name: 'Tiers'})).toHaveAttribute(
    'aria-current',
    'true',
  );
});
