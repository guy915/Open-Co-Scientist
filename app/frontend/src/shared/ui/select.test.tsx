import {fireEvent, render, screen} from '@testing-library/react';
import {useRef} from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {useAnchoredMenu} from './select';

afterEach(() => vi.restoreAllMocks());

function Harness() {
  const anchor = useRef<HTMLDivElement>(null);
  const {menuRef, menuStyle} = useAnchoredMenu(true, anchor, 'end');
  return (
    <div data-testid="dialog">
      <div ref={anchor} data-testid="anchor" />
      <div ref={menuRef} style={menuStyle} data-testid="menu" />
    </div>
  );
}

it.each(['transitionend', 'transitioncancel'])(
  'updates the fixed origin when ancestor motion emits %s',
  eventName => {
    let origin = 80;
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
      function (this: HTMLElement) {
        return DOMRect.fromRect({
          x: this.dataset.testid === 'anchor' ? 200 : origin,
          y: 20,
          width: 200,
          height: 40,
        });
      },
    );
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(40);
    const {unmount} = render(<Harness />);
    const menu = screen.getByTestId('menu');
    expect(menu.style.left).toBe('120px');
    origin = 100;
    fireEvent(
      screen.getByTestId('dialog'),
      new Event(eventName, {bubbles: true}),
    );
    expect(menu.style.left).toBe('100px');
    unmount();
    origin = 120;
    fireEvent(window, new Event(eventName));
    expect(menu.style.left).toBe('100px');
  },
);
