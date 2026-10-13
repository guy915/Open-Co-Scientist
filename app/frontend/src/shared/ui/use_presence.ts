import {useEffect, useState} from 'react';

// Matches --motion-duration-exit; the timer only decides when to unmount, the
// stylesheet owns the visible exit.
export const EXIT_MS = 150;

export type PresenceState = 'open' | 'closed';

// Keeps a closing element mounted for its exit transition. Callers drop focus
// handling and inert state at once; only the visuals linger.
export function usePresence(open: boolean): {
  mounted: boolean;
  state: PresenceState;
} {
  const [mounted, setMounted] = useState(open);
  if (open && !mounted) setMounted(true);

  useEffect(() => {
    if (open || !mounted) return;
    const timer = window.setTimeout(() => setMounted(false), EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [open, mounted]);

  return {mounted: open || mounted, state: open ? 'open' : 'closed'};
}

// Spread on an element that is leaving: hidden from assistive technology and
// from interaction while it fades.
export function presenceProps(state: PresenceState): {
  'data-state': PresenceState;
  'aria-hidden'?: true;
  inert?: true;
} {
  return state === 'open'
    ? {'data-state': 'open'}
    : {'data-state': 'closed', 'aria-hidden': true, inert: true};
}
