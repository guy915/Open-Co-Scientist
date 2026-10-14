import {matchesMedia} from './media';

export const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

export function prefersReducedMotion(): boolean {
  return matchesMedia(REDUCED_MOTION_QUERY);
}

// Programmatic scrolls jump instead of gliding when motion is reduced.
export function scrollBehavior(): ScrollBehavior {
  return prefersReducedMotion() ? 'auto' : 'smooth';
}
