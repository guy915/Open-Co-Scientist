import {useEffect, useRef, type RefObject} from 'react';

// One outside-pointer and Escape policy for every popup. Listeners exist only
// while open, and Escape stops at the innermost popup so a menu inside a
// dialog closes before the dialog does.
export function useDismiss(
  open: boolean,
  onDismiss: () => void,
  insideRefs: RefObject<HTMLElement | null>[],
): void {
  const latest = useRef({onDismiss, insideRefs});
  latest.current = {onDismiss, insideRefs};

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node;
      const inside = latest.current.insideRefs.some(ref =>
        ref.current?.contains(target),
      );
      if (!inside) latest.current.onDismiss();
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      latest.current.onDismiss();
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);
}
