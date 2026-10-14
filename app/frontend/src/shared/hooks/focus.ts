import {useEffect, useRef, type RefObject} from 'react';
import {logModalOpen} from '@/shared/lib/ui_logging';

// Restore only siblings this pass made inert; nested dialogs or independent
// owners may already control the others.
function inertSiblingsOf(node: Element): Element[] {
  const parent = node.parentElement;
  if (!parent) return [];
  const touched: Element[] = [];
  for (const sibling of Array.from(parent.children)) {
    if (sibling === node || sibling.hasAttribute('inert')) continue;
    sibling.setAttribute('inert', '');
    touched.push(sibling);
  }
  return touched;
}

function inertAncestorSiblings(start: Element): Element[] {
  const touched: Element[] = [];
  let node: Element | null = start;
  while (node && node !== document.body) {
    touched.push(...inertSiblingsOf(node));
    node = node.parentElement;
  }
  return touched;
}

// jsdom implements inert attributes but not blocked interaction; tests can prove
// attributes, not browser behavior.
export function useBackgroundInert(
  containerRef: RefObject<HTMLElement | null>,
  label = 'Dialog',
): void {
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const dialog = container.matches('[role="dialog"]')
      ? container
      : container.querySelector('[role="dialog"]');
    logModalOpen(dialog?.getAttribute('aria-label') ?? label);
    const touched = inertAncestorSiblings(container);
    return () => {
      for (const element of touched) element.removeAttribute('inert');
    };
  }, [containerRef, label]);
}

export function useEscapeKey(onEscape: () => void, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onEscape();
    }
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [onEscape, enabled]);
}

// The opener’s popover can unmount before close; capture surviving ancestors so
// restoring focus does not silently fall back to body.
function focusChain(start: Element | null): HTMLElement[] {
  const chain: HTMLElement[] = [];
  let node = start as HTMLElement | null;
  while (node) {
    chain.push(node);
    node = node.parentElement;
  }
  return chain;
}

// Fallback containers need programmatic focus without becoming ordinary Tab
// stops.
function focusEvenIfInert(target: HTMLElement): void {
  target.focus();
  if (document.activeElement === target) return;
  target.setAttribute('tabindex', '-1');
  target.focus();
}

// Capture before any mount effect moves focus; restore a surviving ancestor when
// the original opener has unmounted.
export function useRestoreFocusOnClose(): void {
  const chainRef = useRef<HTMLElement[]>([]);

  useEffect(() => {
    if (!chainRef.current.length) {
      chainRef.current = focusChain(document.activeElement);
    }
    return () => {
      const target = chainRef.current.find(el => el.isConnected);
      if (!target) return;
      // Sibling inert cleanup may run after this effect. Restore after that
      // cleanup rather than changing tabindex on a still-blocked opener.
      if (target.closest('[inert]')) {
        queueMicrotask(() => {
          if (target.isConnected && !target.closest('[inert]')) {
            focusEvenIfInert(target);
          }
        });
      } else {
        focusEvenIfInert(target);
      }
    };
  }, []);
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  );
}

function includesActive(
  focusable: HTMLElement[],
  active: Element | null,
): boolean {
  return active !== null && focusable.includes(active as HTMLElement);
}

function isAtOrPastLast(
  focusable: HTMLElement[],
  active: Element | null,
): boolean {
  return (
    active === focusable[focusable.length - 1] ||
    !includesActive(focusable, active)
  );
}

function isAtOrPastFirst(
  focusable: HTMLElement[],
  active: Element | null,
): boolean {
  return active === focusable[0] || !includesActive(focusable, active);
}

function nextTrapTarget(
  focusable: HTMLElement[],
  active: Element | null,
  shiftKey: boolean,
): HTMLElement | null {
  if (focusable.length === 0) return null;
  const wrapping = shiftKey
    ? isAtOrPastFirst(focusable, active)
    : isAtOrPastLast(focusable, active);
  if (!wrapping) return null;
  return shiftKey ? focusable[focusable.length - 1] : focusable[0];
}

export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
): void {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Tab') return;
      const container = containerRef.current;
      if (!container) return;
      const focusable = focusableElements(container);
      if (focusable.length === 0) {
        event.preventDefault();
        return;
      }
      const target = nextTrapTarget(
        focusable,
        document.activeElement,
        event.shiftKey,
      );
      if (!target) return;
      event.preventDefault();
      target.focus();
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [containerRef]);
}
