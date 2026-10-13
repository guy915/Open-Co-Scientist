import {useCallback, useSyncExternalStore} from 'react';
import {matchesMedia, watchMedia} from '@/shared/lib/media';
import {REDUCED_MOTION_QUERY} from '@/shared/lib/reduced_motion';

// createRoot mounts without hydration, so the first render already reads the
// live query and phones never paint one frame of desktop layout.
export function useMediaQuery(query: string, fallback = false): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => watchMedia(query, onChange),
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => matchesMedia(query, fallback),
    () => fallback,
  );
}

export function useReducedMotion(): boolean {
  return useMediaQuery(REDUCED_MOTION_QUERY);
}
