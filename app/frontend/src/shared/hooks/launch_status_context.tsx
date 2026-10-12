import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {getLaunchStatus, type LaunchStatus} from '@/shared/api/launch_control';
import {resolveByokRoutes} from '@/shared/lib/client_id';
import {usePoll} from './timers';

export const LaunchStatusContext = createContext<LaunchStatus | null>(null);

export function LaunchStatusProvider({children}: {children: ReactNode}) {
  const [status, setStatus] = useState<LaunchStatus | null>(null);
  const live = useRef(false);
  const pending = useRef(false);
  const refresh = useCallback(async () => {
    if (pending.current) return;
    pending.current = true;
    try {
      const next = await getLaunchStatus();
      if (live.current) setStatus(next);
    } catch {
      // Keep the last known refusal during a transient transport failure.
    } finally {
      pending.current = false;
    }
  }, []);
  useEffect(() => {
    live.current = true;
    void refresh();
    return () => {
      live.current = false;
    };
  }, [refresh]);
  usePoll(() => void refresh(), 3000);
  return (
    <LaunchStatusContext.Provider value={status}>
      {children}
    </LaunchStatusContext.Provider>
  );
}

export function useLaunchStatus() {
  const status = useContext(LaunchStatusContext);
  const byok = resolveByokRoutes() !== null;
  return {
    status,
    runBlocked: status
      ? !(byok ? status.byok_runs_allowed : status.free_runs_allowed)
      : false,
  };
}
