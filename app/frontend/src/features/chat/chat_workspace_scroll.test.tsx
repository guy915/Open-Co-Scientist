import {useRef, useState} from 'react';
import {act, render} from '@testing-library/react';
import {beforeEach, expect, test, vi} from 'vitest';
import {useChatTimelineScroll} from './chat_workspace';
import {type TimelineItem} from './chat_workspace_timeline';

// jsdom reports zero heights; give the scroller realistic overflow geometry.
const CONTENT_HEIGHT = 1000;
const WINDOW_HEIGHT = 400;

function makeItems(count: number): TimelineItem[] {
  return Array.from({length: count}, (_, index) => ({
    id: `item-${index}`,
    at: index,
    order: index,
    node: null,
  }));
}

beforeEach(() => {
  vi.useFakeTimers();
});

// Mount before transcript rehydration to expose initial scrolling rather than
// preloaded state.
function renderFreshScroller() {
  let populate: ((items: TimelineItem[]) => void) | undefined;
  function Harness() {
    const [items, setItems] = useState<TimelineItem[]>([]);
    populate = setItems;
    const ref = useChatTimelineScroll(items, null, false, undefined);
    return <div data-testid="scroller" ref={ref} />;
  }
  const {getByTestId} = render(<Harness />);
  const scroller = getByTestId('scroller');
  Object.defineProperty(scroller, 'scrollHeight', {value: CONTENT_HEIGHT});
  Object.defineProperty(scroller, 'clientHeight', {value: WINDOW_HEIGHT});
  return {
    scroller,
    populate: (items: TimelineItem[]) => act(() => populate?.(items)),
  };
}

function renderComposerScroller() {
  let populate: ((items: TimelineItem[]) => void) | undefined;
  let composerHeight = 120;
  const resizeCallbacks = new Map<Element, () => void>();
  class ControlledResizeObserver {
    constructor(private readonly callback: ResizeObserverCallback) {}

    observe(target: Element) {
      resizeCallbacks.set(target, () =>
        this.callback([], this as unknown as ResizeObserver),
      );
    }

    disconnect() {}
  }
  vi.stubGlobal('ResizeObserver', ControlledResizeObserver);

  function Harness() {
    const [items, setItems] = useState<TimelineItem[]>([]);
    populate = setItems;
    const composerRef = useRef<HTMLDivElement>(null);
    const ref = useChatTimelineScroll(
      items,
      null,
      false,
      undefined,
      null,
      composerRef,
      true,
    );
    return (
      <>
        <div data-testid="scroller" ref={ref}>
          <div />
        </div>
        <div data-testid="composer" ref={composerRef} />
      </>
    );
  }

  const {getByTestId} = render(<Harness />);
  const scroller = getByTestId('scroller');
  const composer = getByTestId('composer');
  Object.defineProperty(scroller, 'scrollHeight', {
    configurable: true,
    get: () => CONTENT_HEIGHT + composerHeight,
  });
  Object.defineProperty(scroller, 'clientHeight', {
    configurable: true,
    value: WINDOW_HEIGHT,
  });
  Object.defineProperty(composer, 'offsetHeight', {
    configurable: true,
    get: () => composerHeight,
  });
  return {
    scroller,
    populate: (items: TimelineItem[]) => act(() => populate?.(items)),
    resizeComposer: (height: number) => {
      composerHeight = height;
      act(() => {
        resizeCallbacks.get(composer)?.();
        vi.advanceTimersToNextFrame();
      });
    },
  };
}

test('lands at the bottom the first time a fresh mount gets real content', () => {
  const {scroller, populate} = renderFreshScroller();

  populate(makeItems(20));
  act(() => void vi.runAllTimers());

  expect(scroller.scrollTop).toBe(CONTENT_HEIGHT);
});

test('follows composer growth while pinned and preserves an intentional scroll-up', () => {
  const {scroller, populate, resizeComposer} = renderComposerScroller();
  populate(makeItems(20));
  act(() => void vi.runAllTimers());

  expect(scroller.scrollTop).toBe(CONTENT_HEIGHT + 120);
  resizeComposer(240);
  expect(scroller.scrollTop).toBe(CONTENT_HEIGHT + 240);
  expect(scroller.style.getPropertyValue('--chat-composer-h')).toBe('240px');

  scroller.dispatchEvent(
    new WheelEvent('wheel', {bubbles: true, deltaY: -120}),
  );
  scroller.scrollTop = 500;
  resizeComposer(320);
  expect(scroller.scrollTop).toBe(500);
});
