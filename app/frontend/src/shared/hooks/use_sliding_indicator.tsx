import {type RefObject, useLayoutEffect, useState} from 'react';

export interface IndicatorBox {
  left: number;
  width: number;
  // Place the indicator without animating its first appearance.
  animate: boolean;
}

export function useSlidingIndicator(
  trackRef: RefObject<HTMLElement | null>,
  selector: string,
  selected: string,
): IndicatorBox | null {
  const [box, setBox] = useState<IndicatorBox | null>(null);
  useLayoutEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    const measure = () => {
      const item = track.querySelector<HTMLElement>(selector);
      if (!item) return;
      setBox(prev => ({
        left: item.offsetLeft,
        width: item.offsetWidth,
        animate: prev !== null,
      }));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(track);
    return () => observer.disconnect();
  }, [trackRef, selector, selected]);
  return box;
}
