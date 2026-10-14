import {type RefObject, useEffect, useState, useCallback, useRef} from 'react';

export interface MotionProps {
  reduceMotion: boolean;
}

// Without IntersectionObserver, report off screen so animations cannot run
// unobserved.
export function useInView(
  ref: RefObject<Element | null>,
  {once = true, margin = '0px', threshold = 0.15} = {},
): boolean {
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (once && !entry.isIntersecting) return;
        setInView(entry.isIntersecting);
        if (once) observer.disconnect();
      },
      {rootMargin: margin, threshold},
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, once, margin, threshold]);
  return inView;
}

// The home scrolls inside a shell pane; target its scrolling ancestor rather
// than assuming the window.
export function scrollParent(el: Element | null): HTMLElement | Window {
  for (let node = el?.parentElement; node; node = node.parentElement) {
    const {overflowY} = getComputedStyle(node);
    if (overflowY === 'auto' || overflowY === 'scroll') return node;
  }
  return window;
}

// Sample all shapes at equal point counts so pointwise interpolation can morph
// any pair.

type Point = readonly [number, number];

const SAMPLES = 180;
const TAU = Math.PI * 2;

function polar(radius: (angle: number) => number): Point[] {
  return Array.from({length: SAMPLES}, (_, i) => {
    const angle = (i / SAMPLES) * TAU - Math.PI / 2;
    const r = radius(angle);
    return [50 + r * Math.cos(angle), 50 + r * Math.sin(angle)] as const;
  });
}

function pill(halfWidth: number, halfHeight: number): Point[] {
  const p = 8;
  return Array.from({length: SAMPLES}, (_, i) => {
    const angle = (i / SAMPLES) * TAU;
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    const k = Math.pow(
      Math.pow(Math.abs(c) / halfWidth, p) +
        Math.pow(Math.abs(s) / halfHeight, p),
      -1 / p,
    );
    return [50 + k * c, 50 + k * s] as const;
  });
}

const SHAPES = {
  circle: polar(() => 46),
  cookie12: polar(a => 44 + 3.2 * Math.cos(12 * a)),
  cookie7: polar(a => 43 + 4.2 * Math.cos(7 * a)),
  flower: polar(a => 40 + 8 * Math.pow(Math.abs(Math.cos(4 * a)), 0.6)),
  clover: polar(a => 36 + 12 * Math.pow(Math.abs(Math.cos(2 * a)), 0.9)),
  sunny: polar(a => 44 + 3.4 * Math.cos(8 * a + Math.PI)),
  gem: polar(a => 44 - 5 * Math.pow(Math.abs(Math.sin(3 * a)), 1.2)),
  pill: pill(47, 30),
};

export type ShapeName = keyof typeof SHAPES;

function toPath(points: readonly Point[]): string {
  return (
    'M' +
    points.map(([x, y]) => `${x.toFixed(2)} ${y.toFixed(2)}`).join('L') +
    'Z'
  );
}

export function shapePath(name: ShapeName): string {
  return toPath(SHAPES[name]);
}

function blendShapes(from: ShapeName, to: ShapeName, k: number): string {
  const a = SHAPES[from];
  const b = SHAPES[to];
  return toPath(
    a.map(([x, y], i) => [x + (b[i][0] - x) * k, y + (b[i][1] - y) * k]),
  );
}

const MORPH_MS = 700;

// Write the path per frame rather than rerender React for every morph step.
export function useShapeMorph(
  rest: ShapeName,
  reduceMotion: boolean,
): {
  pathRef: RefObject<SVGPathElement | null>;
  toCircle: () => void;
  toRest: () => void;
} {
  const pathRef = useRef<SVGPathElement | null>(null);
  // A newer morph orphans the older animation token.
  const token = useRef(0);
  const run = useCallback(
    (from: ShapeName, to: ShapeName) => {
      const path = pathRef.current;
      if (!path) return;
      const id = ++token.current;
      if (reduceMotion) {
        path.setAttribute('d', shapePath(to));
        return;
      }
      const start = performance.now();
      const step = (now: number) => {
        if (token.current !== id) return;
        const t = Math.min(1, (now - start) / MORPH_MS);
        path.setAttribute('d', blendShapes(from, to, 1 - Math.pow(1 - t, 4)));
        if (t < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    [reduceMotion],
  );
  return {
    pathRef,
    toCircle: useCallback(() => run(rest, 'circle'), [run, rest]),
    toRest: useCallback(() => run('circle', rest), [run, rest]),
  };
}
