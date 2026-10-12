import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {getSystemStatus, type SystemStatus} from '@/shared/api/system';
import {usePoll} from './timers';

const STATUS_REFRESH_MS = 60_000;

interface SystemStatusState {
  status: SystemStatus | null;
  unreachable: boolean;
}

const SystemStatusContext = createContext<SystemStatusState | null>(null);

function usePolledSystemStatus(enabled = true): SystemStatusState {
  const [state, setState] = useState<SystemStatusState>({
    status: null,
    unreachable: false,
  });
  // Set on mount too: StrictMode remounts after a simulated unmount.
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const refresh = useCallback(async () => {
    try {
      const status = await getSystemStatus();
      if (live.current) setState({status, unreachable: false});
    } catch {
      if (live.current) setState(current => ({...current, unreachable: true}));
    }
  }, []);
  useEffect(() => {
    if (enabled) void refresh();
  }, [enabled, refresh]);
  usePoll(() => void refresh(), STATUS_REFRESH_MS, enabled);
  return state;
}

// Shell controls share one persistent poll instead of restarting it when their
// menus open.
export function SystemStatusProvider({children}: {children: ReactNode}) {
  const state = usePolledSystemStatus();
  return (
    <SystemStatusContext.Provider value={state}>
      {children}
    </SystemStatusContext.Provider>
  );
}

export function useSystemStatus(): SystemStatusState {
  const shared = useContext(SystemStatusContext);
  // Standalone consumers still need polling when no shell provider owns the
  // shared read.
  const local = usePolledSystemStatus(shared === null);
  return shared ?? local;
}
