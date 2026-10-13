import {
  useEffect,
  useState,
  useCallback,
  useRef,
  useSyncExternalStore,
} from 'react';
import {nowSeconds} from '@/shared/lib/time';

function subscribeVisibility(onChange: () => void): () => void {
  document.addEventListener('visibilitychange', onChange);
  return () => document.removeEventListener('visibilitychange', onChange);
}

function usePageVisible(): boolean {
  return useSyncExternalStore(
    subscribeVisibility,
    () => document.visibilityState !== 'hidden',
    () => true,
  );
}

// A hidden tab polls nothing (`/status` runs probes); returning to it polls at
// once instead of showing data as old as the time away.
export function usePoll(
  poll: () => void,
  intervalMs: number,
  enabled = true,
): void {
  const visible = usePageVisible();
  const latest = useRef(poll);
  useEffect(() => {
    latest.current = poll;
  });
  const missed = useRef(false);
  useEffect(() => {
    if (!enabled) return;
    if (!visible) {
      missed.current = true;
      return;
    }
    if (missed.current) {
      missed.current = false;
      latest.current();
    }
    const id = window.setInterval(() => latest.current(), intervalMs);
    return () => window.clearInterval(id);
  }, [enabled, visible, intervalMs]);
}

// Only clocks that are still counting need to tick.
export function useNowTick(intervalMs: number, enabled = true): number {
  const [now, setNow] = useState(nowSeconds);
  usePoll(() => setNow(nowSeconds()), intervalMs, enabled);
  return now;
}

// Replace pending expiry so repeated actions receive a full window; drop
// callbacks on unmount.
export function useResetTimer(): {
  schedule: (run: () => void, delayMs: number) => void;
  cancel: () => void;
} {
  const timerRef = useRef<number | null>(null);

  const cancel = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
  }, []);

  const schedule = useCallback(
    (run: () => void, delayMs: number) => {
      cancel();
      timerRef.current = window.setTimeout(run, delayMs);
    },
    [cancel],
  );

  useEffect(() => cancel, [cancel]);

  return {schedule, cancel};
}

interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastState {
  message: string;
  action?: ToastAction;
}

export type ToastSetter = (value: string | ToastState | null) => void;

export function useToast(durationMs = 3000): {
  toast: ToastState | null;
  setToast: ToastSetter;
} {
  const [toast, setToastState] = useState<ToastState | null>(null);

  // Keep setter identity stable because consumers use it in dependencies and
  // child props.
  const setToast = useCallback((value: string | ToastState | null) => {
    setToastState(
      value === null
        ? null
        : typeof value === 'string'
          ? {message: value}
          : value,
    );
  }, []);

  // Replacement toast identity restarts its full expiry; cleanup prevents old
  // timers clearing later toasts or firing after unmount.
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToastState(null), durationMs);
    return () => window.clearTimeout(timer);
  }, [toast, durationMs]);

  return {toast, setToast};
}
