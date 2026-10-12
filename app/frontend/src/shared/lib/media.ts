// Prerendering has no window, and jsdom has no matchMedia: callers say what a
// query reads as there.
export function matchesMedia(query: string, fallback = false): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function')
    return fallback;
  return window.matchMedia(query).matches;
}

// Older Safari lacks MediaQueryList add/removeEventListener.
export function watchMedia(query: string, onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function')
    return () => undefined;
  const media = window.matchMedia(query);
  if (media.addEventListener) {
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }
  if (media.addListener) {
    media.addListener(onChange);
    return () => media.removeListener(onChange);
  }
  return () => undefined;
}
