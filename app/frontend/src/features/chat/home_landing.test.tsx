import {fireEvent, render, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import HomeLanding from './home_landing';
import {shapePath, useShapeMorph} from './home_landing_hooks';

const scrollIntoView = vi.fn();
const scrollTo = vi.fn();

beforeEach(() => {
  Element.prototype.scrollIntoView = scrollIntoView;
  vi.stubGlobal('scrollTo', scrollTo);
});

afterEach(() => {
  scrollIntoView.mockReset();
  scrollTo.mockReset();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

function renderLanding(path = '/', container?: HTMLElement) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <HomeLanding />
    </MemoryRouter>,
    {container},
  );
}

// The rail joins the page header only when the header leaves room around its
// centre; jsdom measures nothing, so give the header a wide box.
function renderInHeaderPane() {
  const header = document.createElement('header');
  const slot = document.createElement('div');
  slot.id = 'header-landing-tabs';
  header.append(slot);
  vi.spyOn(header, 'getBoundingClientRect').mockReturnValue({
    left: 0,
    right: 1000,
    top: 0,
    bottom: 60,
    width: 1000,
    height: 60,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  });
  const pane = document.createElement('div');
  pane.className = 'ucs-page--home';
  const paneScrollTo = vi.fn();
  (pane as unknown as {scrollTo: typeof paneScrollTo}).scrollTo = paneScrollTo;
  document.body.append(header, pane);
  renderLanding('/', pane);
  return {slot, pane, paneScrollTo};
}

it('opens on the FAQ when the address asks for it', () => {
  vi.useFakeTimers();
  renderLanding('/#faq');
  vi.advanceTimersByTime(100);
  vi.useRealTimers();
  expect(scrollIntoView).toHaveBeenCalled();
  expect(scrollIntoView.mock.contexts.at(-1)).toBe(
    document.getElementById('faq'),
  );
});

it('moves the landing tabs into the page header when it has room', () => {
  const {slot, pane, paneScrollTo} = renderInHeaderPane();

  const headerTab = within(slot).getByRole('link', {
    name: 'Safety',
    hidden: true,
  });
  fireEvent.click(headerTab);
  fireEvent.scroll(pane);
  fireEvent.click(
    within(pane).getAllByRole('button', {name: 'Start a research goal'})[0],
  );

  expect(paneScrollTo).toHaveBeenCalledWith(
    expect.objectContaining({behavior: 'smooth'}),
  );
  expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({top: 0}));
});

it('morphs the shape to a circle when the visitor prefers reduced motion', () => {
  vi.stubGlobal('requestAnimationFrame', (step: (now: number) => void) =>
    step(performance.now() + 1000),
  );
  function Shape() {
    const {pathRef, toCircle} = useShapeMorph('pill', true);
    return (
      <svg>
        <path ref={pathRef} d={shapePath('pill')} onClick={toCircle} />
      </svg>
    );
  }
  const {container} = render(<Shape />);
  const path = container.querySelector('path')!;

  path.dispatchEvent(new MouseEvent('click', {bubbles: true}));

  expect(path.getAttribute('d')).toBe(shapePath('circle'));
});
