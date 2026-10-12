import {useState} from 'react';
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

test('lands at the bottom the first time a fresh mount gets real content', () => {
  const {scroller, populate} = renderFreshScroller();

  populate(makeItems(20));
  act(() => void vi.runAllTimers());

  expect(scroller.scrollTop).toBe(CONTENT_HEIGHT);
});
