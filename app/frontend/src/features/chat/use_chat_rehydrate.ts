import {
  recoverySpecForRun,
  type LinkedRunTarget,
  type PendingRunCreatePayload,
} from '@/shared/lib/run_spec';
import {useCallback, useEffect, useRef, useState} from 'react';
import {
  type Interview,
  getRun,
  getInterview,
  getRunMessages,
  isCancelledStatus,
  isDraftStatus,
  type ChatSummary,
  type Run,
  type RunMessage,
} from '@/shared/api/runs';
import {type InferredRunSpec} from '@/shared/lib/run_spec';
import type {StartedSession} from './chat_timeline_run_spec_card';
import {useChatHistoryContext} from '@/shared/hooks/history_context';
import {useRunHistoryContext} from '@/shared/hooks/history_context';
import {
  applyInterview,
  qaMessagesToEntries,
  runStartAnnouncement,
  type RehydratedAnnouncement,
} from './chat_session_transcript';
import {readPendingCreateIntent} from './chat_session_start_run';
import type {LinkedDraftRecovery, useChatSession} from './use_chat_session';
import {displayTitle} from '@/shared/lib/titles';

type ChatSession = ReturnType<typeof useChatSession>;

interface RunResolution {
  chatId: string;
  runId: string;
  chat: ChatSummary | undefined;
  interview: Interview | null;
  run: Run;
  recoverySpec?: InferredRunSpec;
}

interface RunResolutionCallbacks {
  cancelled: () => boolean;
  setLinkedRun: (linkedRun: LinkedRun | null) => void;
  setStartedSession: ChatSession['setStartedSession'];
  setConfirmed: ChatSession['setConfirmed'];
}

async function runForRecovery(
  target: LinkedRunTarget,
  listedRun: Run | undefined,
  refreshStatus: boolean,
): Promise<Run> {
  if (!refreshStatus && listedRun) return listedRun;
  return getRun(target.runId);
}

async function pendingIntentForRun(
  target: LinkedRunTarget,
  run: Run,
): Promise<PendingRunCreatePayload | undefined> {
  if (!isDraftStatus(run.status)) return undefined;
  const intent = await readPendingCreateIntent<PendingRunCreatePayload>(
    target.chatId,
  );
  return intent?.payload;
}

function recoverySpecForStatus(
  target: LinkedRunTarget,
  run: Run,
  intent: PendingRunCreatePayload | undefined,
): InferredRunSpec | undefined {
  if (!isDraftStatus(run.status)) return undefined;
  return recoverySpecForRun(target, run, intent);
}

interface ResolveLinkedRunArgs {
  chatId: string | undefined;
  chats: ChatSummary[];
  history: Run[];
  interview: Interview | null;
  startedSession: StartedSession | null;
  setLinkedRun: (linkedRun: LinkedRun | null) => void;
  setStartedSession: ChatSession['setStartedSession'];
  setConfirmed: ChatSession['setConfirmed'];
}

function startedTitle(run: Run | undefined, fallback: string): string {
  return run
    ? displayTitle(run.title, run.research_goal)
    : displayTitle(null, fallback);
}

function resumedSession(
  chat: ChatSummary,
  run: Run | undefined,
): StartedSession {
  return {
    id: run?.id ?? String(chat.run_id),
    title: startedTitle(run, chat.challenge),
    at: run?.created_at ?? chat.updated_at,
  };
}

function linkedRunSummary(
  chat: ChatSummary | undefined,
  interview: Interview | null,
  chatId: string,
  runId: string,
  run: Run,
): ChatSummary {
  if (chat) return chat;
  const title = interview ? interview.fields.title : null;
  const challenge = interview ? interview.fields.research_challenge : '';
  const status = interview ? interview.status : 'completed';
  return {
    id: chatId,
    title,
    challenge,
    status,
    run_id: runId,
    created_at: run.created_at,
    updated_at: run.updated_at,
  };
}

function applyResolvedRun(
  resolution: RunResolution,
  callbacks: RunResolutionCallbacks,
): void {
  if (callbacks.cancelled()) return;
  const {chatId, runId, chat, interview, run, recoverySpec} = resolution;
  callbacks.setLinkedRun({
    chatId,
    runId,
    phase: 'ready',
    run,
    recoverySpec,
  });
  if (isDraftStatus(run.status) || isCancelledStatus(run.status)) {
    callbacks.setStartedSession(current =>
      current?.id === runId ? null : current,
    );
    return;
  }
  if (typeof run.config.example_source_id === 'string') {
    callbacks.setConfirmed(current =>
      current
        ? {
            ...current,
            spec: recoverySpecForRun(resolution, run, undefined),
          }
        : current,
    );
  }
  const summary = linkedRunSummary(chat, interview, chatId, runId, run);
  callbacks.setStartedSession(current =>
    current?.id === runId ? current : resumedSession(summary, run),
  );
}

function loadLinkedRun(
  target: LinkedRunTarget,
  listedRun: Run | undefined,
  refreshStatus: boolean,
  callbacks: RunResolutionCallbacks,
): void {
  void (async () => {
    const run = await runForRecovery(target, listedRun, refreshStatus);
    const intent = await pendingIntentForRun(target, run);
    const recoverySpec = recoverySpecForStatus(target, run, intent);
    applyResolvedRun({...target, run, recoverySpec}, callbacks);
  })().catch(() => {
    // An inaccessible or deleted linked run cannot authorize creation of a
    // replacement run.
    if (!callbacks.cancelled()) {
      callbacks.setLinkedRun({...target, phase: 'error'});
    }
  });
}

function linkedRunTarget(args: ResolveLinkedRunArgs): LinkedRunTarget | null {
  if (!args.chatId) return null;
  const chat = args.chats.find(entry => entry.id === args.chatId);
  const runId = linkedRunId(args.interview, chat);
  if (!runId) {
    return null;
  }
  return {chatId: args.chatId, runId, chat, interview: args.interview};
}

function linkedRunId(
  interview: Interview | null,
  chat: ChatSummary | undefined,
): string | null {
  if (interview?.run_id) return interview.run_id;
  return chat?.run_id ?? null;
}

function resolveLinkedRun(
  args: ResolveLinkedRunArgs,
  refreshStatus: boolean,
): () => void {
  const target = linkedRunTarget(args);
  if (!target) {
    args.setLinkedRun(null);
    return () => undefined;
  }
  if (args.startedSession?.id === target.runId && !args.interview) {
    args.setLinkedRun(null);
    return () => undefined;
  }

  let cancelled = false;
  const callbacks = {
    cancelled: () => cancelled,
    setLinkedRun: args.setLinkedRun,
    setStartedSession: args.setStartedSession,
    setConfirmed: args.setConfirmed,
  };
  const listedRun = args.history.find(entry => entry.id === target.runId);
  args.setLinkedRun({...target, phase: 'loading'});
  loadLinkedRun(target, listedRun, refreshStatus, callbacks);
  return () => {
    cancelled = true;
  };
}

function interviewForChat(
  session: ChatSession,
  chatId: string | undefined,
): Interview | null {
  return session.interview?.id === chatId ? session.interview : null;
}

function currentRunId(
  interview: Interview | null,
  chats: ChatSummary[],
  chatId: string | undefined,
): string | null {
  if (interview?.run_id) return interview.run_id;
  return chatRunId(chats, chatId ?? '');
}

function alreadyShowing(
  session: ChatSession,
  chatId: string,
  applied: string | null,
): boolean {
  return applied === chatId || session.interview?.id === chatId;
}

// Live chats put their own ID in the URL; leave them untouched rather than
// replacing an ongoing conversation with itself.
export function useChatRehydration(
  session: ChatSession,
  chatId: string | undefined,
): LinkedDraftRecovery & {unavailableChatId: string | null} {
  const {chats} = useChatHistoryContext();
  const {history} = useRunHistoryContext();
  const appliedRef = useRef<string | null>(null);
  // Track Q&A separately because the linked run ID arrives on its own list-
  // loading schedule.
  const qaLoadedRef = useRef<string | null>(null);
  // Announcement state must retrigger merging when either its independent fetch
  // or the session-card load finishes.
  const [announcement, setAnnouncement] =
    useState<RehydratedAnnouncement | null>(null);
  const [linkedRun, setLinkedRun] = useState<LinkedRun | null>(null);
  const [lookupRetry, setLookupRetry] = useState(0);
  const [unavailableChatId, setUnavailableChatId] = useState<string | null>(
    null,
  );
  const retryLinkedRunLookup = useCallback(
    () => setLookupRetry(current => current + 1),
    [],
  );
  const sessionRef = useRef(session);
  sessionRef.current = session;

  useEffect(() => {
    const live = sessionRef.current;
    if (!chatId) {
      if (appliedRef.current) live.resetSession();
      appliedRef.current = null;
      qaLoadedRef.current = null;
      setAnnouncement(null);
      return;
    }
    if (alreadyShowing(live, chatId, appliedRef.current)) return;
    setAnnouncement(null);
    let cancelled = false;
    // Client calls can throw before returning a promise; await inside try/catch
    // also contains that synchronous failure.
    void (async () => {
      try {
        const interview = await getInterview(chatId);
        if (cancelled) return;
        // Mark applied only after adoption; StrictMode cancels its first pass
        // and would otherwise make the second skip an unloaded chat.
        appliedRef.current = chatId;
        applyInterview(sessionRef.current, interview);
      } catch {
        // Missing and other-owner chats are equally inaccessible; leave nothing
        // to reopen and let the rail remove the row on its next refresh.
        if (!cancelled) setUnavailableChatId(chatId);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [chatId]);

  // Interview links commit before start succeeds; read the owned run status
  // before treating its draft as started.
  useEffect(
    () =>
      resolveLinkedRun(
        {
          chatId,
          chats,
          history,
          interview: interviewForChat(sessionRef.current, chatId),
          startedSession: session.startedSession,
          setLinkedRun,
          setStartedSession: sessionRef.current.setStartedSession,
          setConfirmed: sessionRef.current.setConfirmed,
        },
        lookupRetry > 0,
      ),
    [
      chatId,
      chats,
      history,
      lookupRetry,
      session.interview,
      session.startedSession?.id,
    ],
  );

  // Wait until this chat’s transcript replacement lands before appending Q&A, or
  // the independent transcript fetch can erase those bubbles.
  useEffect(() => {
    const live = sessionRef.current;
    if (
      !readyToLoadQa(
        chatId,
        qaLoadedRef.current,
        live.interview?.id,
        appliedRef.current,
      )
    ) {
      return;
    }
    let cancelled = false;
    void loadQaHistory(
      chatId,
      qaRunId(live.interview?.run_id, chats, chatId),
      {qaLoadedRef, live, onAnnouncement: setAnnouncement},
      () => cancelled,
    );
    return () => {
      cancelled = true;
    };
  }, [chatId, chats, session.interview]);

  // The card and announcement load independently; merge only after both exist
  // and return unchanged once filled to avoid an effect loop.
  useEffect(() => {
    if (!announcement) return;
    sessionRef.current.setStartedSession(current =>
      current && current.intro === undefined
        ? {...current, ...announcement}
        : current,
    );
  }, [announcement, session.startedSession]);

  const interview = interviewForChat(session, chatId);
  const runId = currentRunId(interview, chats, chatId);
  const currentLinked = currentLinkedRun(
    chatId,
    runId,
    linkedRun,
    session.startedSession,
  );
  return {
    ...recoverySummary(currentLinked),
    status: recoveryStatus(currentLinked),
    retryStatusLookup: retryLinkedRunLookup,
    unavailableChatId,
  };
}

// Load Q&A only for reopened transcripts; live sessions already contain their
// own persisted exchanges and would duplicate them.
function readyToLoadQa(
  chatId: string | undefined,
  loadedFor: string | null,
  interviewId: string | undefined,
  appliedFor: string | null,
): chatId is string {
  return (
    Boolean(chatId) &&
    loadedFor !== chatId &&
    interviewId === chatId &&
    appliedFor === chatId
  );
}

function chatRunId(chats: ChatSummary[], chatId: string): string | null {
  return chats.find(entry => entry.id === chatId)?.run_id ?? null;
}

// The reopened chat's own run link decides: with none, nothing was saved
// before this tab, and a run started here already shows its start rows, so a
// later list refresh must not append them again. Only a payload without the
// field falls back to the list.
function qaRunId(
  reopenedRunId: string | null | undefined,
  chats: ChatSummary[],
  chatId: string,
): string | null | undefined {
  if (reopenedRunId !== undefined) return reopenedRunId;
  return chatRunId(chats, chatId) ?? undefined;
}

interface QaLoadTarget {
  qaLoadedRef: {current: string | null};
  live: ChatSession;
  onAnnouncement: (announcement: RehydratedAnnouncement) => void;
}

function applyQaRows(
  chatId: string,
  target: QaLoadTarget,
  rows: RunMessage[],
): void {
  target.qaLoadedRef.current = chatId;
  const entries = qaMessagesToEntries(rows);
  if (entries.length) target.live.setMessages(prev => [...prev, ...entries]);
  const announcement = runStartAnnouncement(rows);
  if (announcement) target.onAnnouncement(announcement);
}

// `null` settles the chat as having no saved Q&A; `undefined` waits for the
// chat list to name its run.
async function loadQaHistory(
  chatId: string,
  runId: string | null | undefined,
  target: QaLoadTarget,
  isCancelled: () => boolean,
): Promise<void> {
  if (runId === null) target.qaLoadedRef.current = chatId;
  if (!runId) return;
  try {
    const rows = await getRunMessages(runId);
    if (!isCancelled()) applyQaRows(chatId, target, rows);
  } catch {
    // Q&A is best-effort: its fetch failure must not discard the successfully
    // restored interview.
  }
}

type LinkedRun =
  | {chatId: string; runId: string; phase: 'loading'}
  | {chatId: string; runId: string; phase: 'error'}
  | {
      chatId: string;
      runId: string;
      phase: 'ready';
      run: Run;
      recoverySpec?: InferredRunSpec;
    };

function recoveryStatus(
  linkedRun: LinkedRun | null,
): LinkedDraftRecovery['status'] {
  if (!linkedRun) return undefined;
  if (linkedRun.phase === 'loading') return 'checking';
  if (linkedRun.phase === 'error') return 'error';
  if (isCancelledStatus(linkedRun.run.status)) return 'cancelled';
  return undefined;
}

function recoverySpec(
  linkedRun: LinkedRun | null,
): InferredRunSpec | undefined {
  if (!linkedRun) return undefined;
  if (linkedRun.phase !== 'ready') return undefined;
  return isDraftStatus(linkedRun.run.status)
    ? linkedRun.recoverySpec
    : undefined;
}

function recoverySummary(
  linkedRun: LinkedRun | null,
): Pick<LinkedDraftRecovery, 'canContinueLinkedDraft' | 'spec'> {
  const spec = recoverySpec(linkedRun);
  return {canContinueLinkedDraft: Boolean(spec), spec};
}

function matchesLinkedRun(
  linkedRun: LinkedRun | null,
  chatId: string | undefined,
  runId: string | null,
): linkedRun is LinkedRun {
  return Boolean(
    runId &&
    linkedRun &&
    linkedRun.chatId === chatId &&
    linkedRun.runId === runId,
  );
}

function isCurrentSessionRun(
  startedSession: StartedSession | null,
  runId: string | null,
): boolean {
  return startedSession?.id === runId;
}

function currentLinkedRun(
  chatId: string | undefined,
  runId: string | null,
  linkedRun: LinkedRun | null,
  startedSession: StartedSession | null,
): LinkedRun | null {
  if (!runId) return null;
  if (isCurrentSessionRun(startedSession, runId)) return null;
  if (matchesLinkedRun(linkedRun, chatId, runId)) return linkedRun;
  return {chatId: chatId ?? '', runId, phase: 'loading'};
}
