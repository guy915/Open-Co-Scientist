import {useEffect, useState, useRef} from 'react';
import {matchesMedia} from '@/shared/lib/media';
import {useMediaQuery} from './use_media_query';

// One phone breakpoint prevents seams where related shell and workspace surfaces
// disagree on interaction mode.
export const MOBILE_MEDIA_QUERY = '(max-width: 700px)';

function isMobileViewport(): boolean {
  return matchesMedia(MOBILE_MEDIA_QUERY);
}

// Desktop rail state is a user preference; only an overlaying phone drawer
// should auto-collapse.
export function closeDrawerIfMobile(setNavOpen: (open: boolean) => void): void {
  if (isMobileViewport()) setNavOpen(false);
}

export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_MEDIA_QUERY);
}

// Overflow containers clip tooltips even without scrolling; measure child span,
// not scrollHeight inflated by absolutely positioned tooltips.
export function useOverflowing<T extends HTMLElement>(): [
  React.RefObject<T | null>,
  boolean,
] {
  const ref = useRef<T>(null);
  const [overflowing, setOverflowing] = useState(false);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const first = element.firstElementChild;
      const last = element.lastElementChild;
      if (!first || !last) {
        setOverflowing(false);
        return;
      }
      const span =
        last.getBoundingClientRect().bottom - first.getBoundingClientRect().top;
      // Fractional layout needs one-pixel slack to avoid permanent borderline
      // overflow.
      setOverflowing(span - element.clientHeight > 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    if (element.parentElement) observer.observe(element.parentElement);
    return () => {
      observer.disconnect();
    };
  });

  return [ref, overflowing];
}

// Before measurement, use minimum row height plus gap at the 16px root to keep
// first paint useful.
const FALLBACK_ROW_PITCH_PX = 43.2;

// Hidden or collapsed containers report zero height; retain a useful initial cap
// instead of shrinking to one row.
const UNMEASURED_ROW_COUNT = 10;

function measureRowPitch(rows: Element[]): number {
  if (rows.length >= 2) {
    const first = rows[0].getBoundingClientRect().top;
    const second = rows[1].getBoundingClientRect().top;
    const pitch = second - first;
    if (pitch > 0) return pitch;
  }
  if (rows.length === 1) {
    const height = rows[0].getBoundingClientRect().height;
    if (height > 0) return height;
  }
  return FALLBACK_ROW_PITCH_PX;
}

function fittingRowCount(container: Element, list: Element): number {
  const rows = [...list.children].filter(child =>
    child.hasAttribute('data-fitting-row'),
  );
  const pitch = measureRowPitch(rows);
  const available =
    container.getBoundingClientRect().bottom - list.getBoundingClientRect().top;
  if (available <= 0) return UNMEASURED_ROW_COUNT;
  // Always reserve the Show more slot so its appearance cannot push itself
  // offscreen or oscillate the row count.
  return Math.max(1, Math.floor(available / pitch) - 1);
}

// Measure the flexing container, not content-driven list height; data-fitting-
// row excludes the trailing Show more control.
export function useFittingRows<
  C extends HTMLElement,
  L extends HTMLElement,
>(): {
  containerRef: React.RefObject<C | null>;
  listRef: React.RefObject<L | null>;
  visibleCount: number;
} {
  const containerRef = useRef<C>(null);
  const listRef = useRef<L>(null);
  const [visibleCount, setVisibleCount] = useState(UNMEASURED_ROW_COUNT);

  // Rows load asynchronously, so every committed layout may provide a newly
  // measurable pitch.
  useEffect(() => {
    const container = containerRef.current;
    const list = listRef.current;
    if (!container || !list) return;
    const measure = () => setVisibleCount(fittingRowCount(container, list));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    observer.observe(list);
    return () => {
      observer.disconnect();
    };
  });

  return {containerRef, listRef, visibleCount};
}

// Dialog behaviour moved to the shared building blocks.
export {
  useBackgroundInert,
  useEscapeKey,
  useFocusTrap,
  useRestoreFocusOnClose,
} from '@/shared/hooks/focus';
