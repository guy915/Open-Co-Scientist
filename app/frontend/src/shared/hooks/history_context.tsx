import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  isActiveStatus,
  listInterviews,
  loadRunHistory,
  type ChatSummary,
  type Run,
} from '@/shared/api/runs';
import {CHATS_CHANGED_EVENT, RUNS_CHANGED_EVENT} from '@/shared/lib/dom_events';
import {useLocation} from 'react-router-dom';
import {usePoll} from './timers';

// Undefined load results preserve the current list after transient failure; only
// self-advancing run history needs polling.
function useHistoryList<T>(
  load: () => Promise<T[] | undefined>,
  changedEvent: string,
) {
  const {pathname} = useLocation();
  const [items, setItems] = useState<T[]>([]);
  const mounted = useRef(false);
  const requestSequence = useRef(0);

  const reload = useCallback(async () => {
    if (!mounted.current) return;
    const request = ++requestSequence.current;
    const next = await load();
    if (
      mounted.current &&
      request === requestSequence.current &&
      next !== undefined
    ) {
      setItems(next);
    }
  }, [load]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requestSequence.current += 1;
    };
  }, []);

  useEffect(() => {
    void reload();
  }, [reload, pathname]);

  useEffect(() => {
    window.addEventListener(changedEvent, reload);
    return () => window.removeEventListener(changedEvent, reload);
  }, [changedEvent, reload]);

  return {items, reload};
}

interface RunHistoryContextValue {
  history: Run[];
  reload: () => Promise<void>;
}

const RunHistoryContext = createContext<RunHistoryContextValue | null>(null);

const ACTIVE_RUN_REFRESH_MS = 10_000;

// Share one fetched run list across shell and home; execution advances without
// navigation or change signals, so active runs also need polling.
export function RunHistoryProvider({children}: {children: ReactNode}) {
  const {items: history, reload} = useHistoryList(
    loadRunHistory,
    RUNS_CHANGED_EVENT,
  );

  // Depend on activity, not the refreshed list identity, so each poll cannot
  // restart its own timer.
  const hasActiveRun = history.some(run => isActiveStatus(run.status));
  usePoll(() => void reload(), ACTIVE_RUN_REFRESH_MS, hasActiveRun);

  return (
    <RunHistoryContext.Provider value={{history, reload}}>
      {children}
    </RunHistoryContext.Provider>
  );
}

export function useRunHistoryContext(): RunHistoryContextValue {
  const ctx = useContext(RunHistoryContext);
  if (!ctx) {
    throw new Error(
      'useRunHistoryContext must be used within a RunHistoryProvider',
    );
  }
  return ctx;
}

interface ChatHistoryContextValue {
  chats: ChatSummary[];
  reload: () => Promise<void>;
}

const ChatHistoryContext = createContext<ChatHistoryContextValue | null>(null);

async function loadChatHistory(): Promise<ChatSummary[] | undefined> {
  try {
    return await listInterviews();
  } catch {
    // Transient failures retain visible chats until a later signal or navigation
    // succeeds.
    return undefined;
  }
}

// Chats exist before any run and need a separate history; browser-authored chat
// changes need signals, not a run-progress timer.
export function ChatHistoryProvider({children}: {children: ReactNode}) {
  const {items: chats, reload} = useHistoryList(
    loadChatHistory,
    CHATS_CHANGED_EVENT,
  );

  return (
    <ChatHistoryContext.Provider value={{chats, reload}}>
      {children}
    </ChatHistoryContext.Provider>
  );
}

export function useChatHistoryContext(): ChatHistoryContextValue {
  const ctx = useContext(ChatHistoryContext);
  if (!ctx) {
    throw new Error(
      'useChatHistoryContext must be used within a ChatHistoryProvider',
    );
  }
  return ctx;
}

export function announceChatsChanged(): void {
  window.dispatchEvent(new Event(CHATS_CHANGED_EVENT));
}
