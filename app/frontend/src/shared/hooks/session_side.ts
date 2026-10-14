import {useEffect} from 'react';
import {useLocation} from 'react-router-dom';
import {runPath} from '@/shared/lib/routes';
import {normalizeTab, TABS, type TabName} from '@/shared/lib/run_tabs';
import {
  removeStorage,
  readStorage,
  STORAGE_KEYS,
  writeStorage,
} from '@/shared/lib/safe_storage';

const ACTIVE_TAB_KEY = STORAGE_KEYS.activeSessionResultTab;

interface SessionTab {
  chatId: string;
  runId: string;
  tab: TabName;
}

function readActiveSessionTab(): SessionTab | null {
  const raw = readStorage('session', ACTIVE_TAB_KEY);
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const candidate = value as Record<string, unknown>;
    if (
      typeof candidate.chatId !== 'string' ||
      typeof candidate.runId !== 'string' ||
      typeof candidate.tab !== 'string' ||
      !(TABS as readonly string[]).includes(candidate.tab)
    ) {
      return null;
    }
    return {
      chatId: candidate.chatId,
      runId: candidate.runId,
      tab: candidate.tab as TabName,
    };
  } catch {
    return null;
  }
}

function clearActiveSessionTab(): void {
  removeStorage('session', ACTIVE_TAB_KEY);
}

export function resultsPath(runId: string, chatId?: string): string {
  const active = chatId ? readActiveSessionTab() : null;
  const tab =
    active && chatId && active.chatId === chatId && active.runId === runId
      ? active.tab
      : undefined;
  return runPath(runId, tab);
}

export function useRecordRunTab(
  session: {chatId: string; runId: string} | null,
): void {
  const {pathname} = useLocation();
  useEffect(() => {
    const [, section, runId, tab] = pathname.split('/');
    if (section === 'runs' && runId && tab) {
      const active = readActiveSessionTab();
      if (session?.runId === runId) {
        writeStorage(
          'session',
          ACTIVE_TAB_KEY,
          JSON.stringify({
            chatId: session.chatId,
            runId,
            tab: normalizeTab(tab),
          }),
        );
      } else if (active?.runId !== runId) {
        clearActiveSessionTab();
      }
      return;
    }
    if (section === 'chats' && runId) {
      const active = readActiveSessionTab();
      if (
        (session && session.chatId !== runId) ||
        (active && active.chatId !== runId)
      ) {
        clearActiveSessionTab();
      }
      return;
    }
    clearActiveSessionTab();
  }, [pathname, session?.chatId, session?.runId]);
}
