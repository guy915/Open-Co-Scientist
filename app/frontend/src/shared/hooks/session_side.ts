import {useEffect} from 'react';
import {useLocation} from 'react-router-dom';
import {runPath} from '@/shared/lib/routes';
import {
  readStorage,
  STORAGE_KEYS,
  writeStorage,
} from '@/shared/lib/safe_storage';

const TAB_PREFIX = STORAGE_KEYS.sessionTabPrefix;

// Results reopen on the tab last viewed for that run; runPath falls back to
// the default tab for an unknown or missing one.
export function resultsPath(runId: string): string {
  return runPath(runId, readStorage('local', TAB_PREFIX + runId) ?? undefined);
}

// Runs without a chat have no session switch side, but still keep their tab.
export function useRecordRunTab(): void {
  const {pathname} = useLocation();
  useEffect(() => {
    const [, section, runId, tab] = pathname.split('/');
    if (section === 'runs' && runId && tab) {
      writeStorage('local', TAB_PREFIX + runId, tab);
    }
  }, [pathname]);
}
