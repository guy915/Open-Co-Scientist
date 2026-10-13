import {act, render} from '@testing-library/react';
import {afterAll, beforeAll, expect, it} from 'vitest';
import {TruncatedLabel} from './truncated_label';

// jsdom measurements are zero; character counts provide deterministic overflow
// geometry.
const CONTAINER_WIDTH = 20;
const CONTAINER_HEIGHT = 10;
const LAYOUT_PROPERTIES = [
  'scrollWidth',
  'clientWidth',
  'scrollHeight',
  'clientHeight',
] as const;
let originalDescriptors: Record<string, PropertyDescriptor | undefined>;

beforeAll(() => {
  originalDescriptors = Object.fromEntries(
    LAYOUT_PROPERTIES.map(key => [
      key,
      Object.getOwnPropertyDescriptor(HTMLElement.prototype, key),
    ]),
  );
  const textLength = {
    configurable: true,
    get(this: HTMLElement) {
      return (this.textContent ?? '').length;
    },
  };
  Object.defineProperty(HTMLElement.prototype, 'scrollWidth', textLength);
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', textLength);
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    value: CONTAINER_WIDTH,
  });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    value: CONTAINER_HEIGHT,
  });
});

afterAll(() => {
  for (const [key, descriptor] of Object.entries(originalDescriptors)) {
    if (descriptor) {
      Object.defineProperty(HTMLElement.prototype, key, descriptor);
    }
  }
});

async function flushNextFrame() {
  await act(async () => {
    await new Promise(resolve => requestAnimationFrame(resolve));
  });
}

it('renders text unchanged when it fits within the container', async () => {
  const {container} = render(<TruncatedLabel text="short label" />);
  await flushNextFrame();
  expect(container.querySelector('span')!.textContent).toBe('short label');
});

it('truncates on a word boundary and appends an ellipsis', async () => {
  const text = 'abc def ghi jkl mno pqr stu vwx yz1 234';
  const {container} = render(<TruncatedLabel text={text} />);
  await flushNextFrame();
  expect(container.querySelector('span')!.textContent).toBe(
    'abc def ghi jkl mno…',
  );
});

it('drops punctuation left before the ellipsis', async () => {
  const {container} = render(
    <TruncatedLabel text="Map one target, followed by more" />,
  );
  await flushNextFrame();
  expect(container.querySelector('span')!.textContent).toBe('Map one target…');
});
