import {expect, it, vi, afterEach} from 'vitest';
import {smoothScrollToSection} from './smooth_scroll';

afterEach(() => {
  document.body.replaceChildren();
});

// jsdom has no layout; stub the measurements used by scrolling.
function stubRect(el: HTMLElement, rect: Partial<{top: number; left: number}>) {
  vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
    top: rect.top ?? 0,
    left: rect.left ?? 0,
    bottom: 0,
    right: 0,
    width: 0,
    height: 0,
    x: rect.left ?? 0,
    y: rect.top ?? 0,
    toJSON() {
      return this;
    },
  });
}

it('scrolls a preferred pane container and returns true', () => {
  const pane = document.createElement('div');
  pane.className = 'idea-detail-pane';
  const target = document.createElement('div');
  target.id = 'section-1';
  pane.append(target);
  document.body.append(pane);

  stubRect(pane, {top: 20});
  stubRect(target, {top: 120});
  // jsdom does not implement Element.scrollTo.
  const scrollTo = vi.fn();
  (pane as unknown as {scrollTo: typeof scrollTo}).scrollTo = scrollTo;
  pane.scrollTop = 50;

  const result = smoothScrollToSection(
    'section-1',
    0,
    '.idea-detail-pane, .cosci-report-scroll',
  );

  expect(result).toBe(true);
  expect(scrollTo).toHaveBeenCalledWith({
    top: 50 + 120 - 20 - 0,
    behavior: 'smooth',
  });
});

it('falls back to scrollIntoView when no scrollable ancestor is found', () => {
  const target = document.createElement('div');
  target.id = 'section-5';
  document.body.append(target);

  const scrollIntoView = vi.fn();
  target.scrollIntoView = scrollIntoView;

  const result = smoothScrollToSection('section-5');

  expect(result).toBe(true);
  expect(scrollIntoView).toHaveBeenCalledWith({
    behavior: 'smooth',
    block: 'start',
  });
});
