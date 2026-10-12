import {lazy, memo, Suspense, useEffect, useRef, useState} from 'react';

const HomeLanding = lazy(() =>
  import('./home_landing').then(module => ({default: memo(module.default)})),
);

export function DeferredHomeLanding() {
  const marker = useRef<HTMLDivElement>(null);
  const [requested, setRequested] = useState(false);
  useEffect(() => {
    const element = marker.current;
    if (!element) return;
    if (typeof IntersectionObserver === 'undefined') {
      setRequested(true);
      return;
    }
    if (window.location.hash) {
      setRequested(true);
      return;
    }
    const observer = new IntersectionObserver(
      entries => {
        if (entries.some(entry => entry.intersectionRatio > 0)) {
          setRequested(true);
          observer.disconnect();
        }
      },
      {threshold: 0.01},
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // The marker preserves the :has(.ucs-landing) home layout before loading.
  const placeholder = (
    <div id="landing" className="ucs-landing min-h-screen" ref={marker} />
  );
  return requested ? (
    <Suspense fallback={placeholder}>
      <HomeLanding />
    </Suspense>
  ) : (
    placeholder
  );
}
