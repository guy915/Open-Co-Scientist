export function afterFirstView(callback: () => void): void {
  const schedule = () => {
    requestAnimationFrame(() => {
      // Let the mounted view discover its fonts before starting background I/O.
      const animations = (document.getAnimations?.() ?? []).filter(animation =>
        Number.isFinite(Number(animation.effect?.getComputedTiming().endTime)),
      );
      void Promise.allSettled([
        document.fonts?.ready ?? Promise.resolve(),
        ...animations.map(animation => animation.finished),
      ]).then(() => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            if (window.requestIdleCallback)
              window.requestIdleCallback(callback, {timeout: 2000});
            else window.setTimeout(callback, 0);
          });
        });
      });
    });
  };
  if (document.readyState === 'complete') schedule();
  else window.addEventListener('load', schedule, {once: true});
}
