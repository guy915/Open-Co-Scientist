import {scrollBehavior} from './reduced_motion';

// Native hash/scrollIntoView can no-op on partly visible sections; explicit
// container coordinates keep rail jumps deterministic.
export function smoothScrollToSection(
  sectionId: string,
  offset = 0,
  preferredSelector?: string,
  behavior: ScrollBehavior = scrollBehavior(),
): boolean {
  const target = document.getElementById(sectionId);
  if (!target) return false;

  const container = findScrollContainer(target, preferredSelector);
  if (container) {
    const targetRect = target.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    container.scrollTo({
      top: container.scrollTop + targetRect.top - containerRect.top - offset,
      behavior,
    });
    return true;
  }

  target.scrollIntoView({behavior, block: 'start'});
  return true;
}

function findPreferredContainer(
  target: HTMLElement,
  preferredSelector?: string,
): HTMLElement | null {
  return preferredSelector
    ? target.closest<HTMLElement>(preferredSelector)
    : null;
}

function isScrollableOverflow(el: HTMLElement): boolean {
  const {overflowY} = getComputedStyle(el);
  return (
    /(auto|scroll|overlay)/.test(overflowY) && el.scrollHeight > el.clientHeight
  );
}

function findScrollContainer(
  target: HTMLElement,
  preferredSelector?: string,
): HTMLElement | null {
  const preferred = findPreferredContainer(target, preferredSelector);
  if (preferred) return preferred;

  let current = target.parentElement;
  while (current) {
    if (isScrollableOverflow(current)) return current;
    current = current.parentElement;
  }
  return null;
}
