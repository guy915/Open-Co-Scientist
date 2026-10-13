import {liveHandlerDeps} from './chat_session_transcript';
import {
  useLayoutEffect,
  useMemo,
  useRef,
  type Dispatch,
  type RefObject,
  type SetStateAction,
  useCallback,
  useReducer,
} from 'react';
import {buildChatHandlers} from './chat_session_handlers';
import type {InferredRunSpec} from '@/shared/lib/run_spec';
import type {Interview, StagedDocument} from '@/shared/api/runs';
import type {ChatEntry} from './chat_timeline_bubble';
import type {StartedSession} from './chat_timeline_run_spec_card';
import type {ToastSetter} from '@/shared/hooks/timers';
import {nowSeconds} from '@/shared/lib/time';

export interface SessionState {
  input: string;
  messages: ChatEntry[];
  interview: Interview | null;
  draft: SpecStage | null;
  confirmed: SpecStage | null;
  startedSession: StartedSession | null;
  isStarting: boolean;
  isAwaitingAgent: boolean;
  agentReasoning: string;
  agentDraft: string;
  error: string | null;
  pendingAttachments: StagedDocument[];
}

export type SessionUpdate =
  Partial<SessionState> | ((current: SessionState) => Partial<SessionState>);

export function initialSessionState(): SessionState {
  return {
    input: '',
    messages: [],
    interview: null,
    draft: null,
    confirmed: null,
    startedSession: null,
    isStarting: false,
    isAwaitingAgent: false,
    agentReasoning: '',
    agentDraft: '',
    error: null,
    pendingAttachments: [],
  };
}

export const clearedLifecycle = {
  interview: null,
  draft: null,
  confirmed: null,
  startedSession: null,
};

export function sessionReducer(
  state: SessionState,
  action: SessionUpdate,
): SessionState {
  const patch = typeof action === 'function' ? action(state) : action;
  return (Object.keys(patch) as (keyof SessionState)[]).some(
    key => !Object.is(state[key], patch[key]),
  )
    ? {...state, ...patch}
    : state;
}

export function fieldSetter<K extends keyof SessionState>(
  update: Dispatch<SessionUpdate>,
  key: K,
): Dispatch<SetStateAction<SessionState[K]>> {
  return value =>
    update(
      current =>
        ({
          [key]: typeof value === 'function' ? value(current[key]) : value,
        }) as Pick<SessionState, K>,
    );
}

export function stageDraftPatch(
  spec: InferredRunSpec,
  createdAt = nowSeconds(),
  intro?: DraftIntro,
): Partial<SessionState> {
  const {message, reasoning, turnId, fallback} = intro ?? {};
  return {
    draft: {spec, createdAt, intro: message, reasoning, turnId, fallback},
    confirmed: null,
    startedSession: null,
  };
}

export function useChatSession(deps: ChatSessionDeps) {
  const [state, update] = useReducer(
    sessionReducer,
    undefined,
    initialSessionState,
  );
  // Stop and reset must cancel immediately, before the next render.
  const turnAbortRef = useRef<AbortController | null>(null);
  const setters = useMemo(
    () => ({
      setInput: fieldSetter(update, 'input'),
      setMessages: fieldSetter(update, 'messages'),
      setInterview: fieldSetter(update, 'interview'),
      setDraft: fieldSetter(update, 'draft'),
      setConfirmed: fieldSetter(update, 'confirmed'),
      setStartedSession: fieldSetter(update, 'startedSession'),
      stageDraftSpec: (
        spec: InferredRunSpec,
        at?: number,
        intro?: DraftIntro,
      ) => update(stageDraftPatch(spec, at, intro)),
    }),
    [update],
  );
  const resetSession = useCallback(() => {
    turnAbortRef.current?.abort();
    turnAbortRef.current = null;
    update(initialSessionState());
  }, [update]);
  const runtime: HandlerDeps = {
    state,
    update,
    services: {...deps},
    turnAbortRef,
  };
  const runtimeRef = useRef(runtime);
  // Aborted renders cannot publish state or services to stable handlers.
  useLayoutEffect(() => {
    runtimeRef.current = runtime;
  });
  const handlers = useMemo(
    () => buildChatHandlers(liveHandlerDeps(runtimeRef)),
    [],
  );
  return {
    input: state.input,
    messages: state.messages,
    interview: state.interview,
    draft: state.draft,
    confirmed: state.confirmed,
    startedSession: state.startedSession,
    isStarting: state.isStarting,
    isAwaitingAgent: state.isAwaitingAgent,
    agentReasoning: state.agentReasoning,
    agentDraft: state.agentDraft,
    error: state.error,
    ...setters,
    ...handlers,
    resetSession,
    hasConversation:
      state.messages.length > 0 ||
      Boolean(
        state.interview ||
        state.draft ||
        state.confirmed ||
        state.startedSession,
      ),
  };
}

// Draft and confirmed stages carry their timestamp and closing turn as one value
// so transitions cannot split them.
export interface SpecStage {
  spec: InferredRunSpec;
  createdAt: number;
  intro?: string;
  reasoning?: string;
  // The plan card has no bubble; retain its durable closing-turn ID so retry
  // addresses the actual persisted answer.
  turnId?: number;
  fallback?: boolean;
}

export interface LinkedDraftRecovery {
  canContinueLinkedDraft: boolean;
  spec?: InferredRunSpec;
  status: 'checking' | 'error' | 'cancelled' | undefined;
  retryStatusLookup: () => void;
}

export interface DraftIntro {
  message?: string;
  reasoning?: string;
  turnId?: number;
  fallback?: boolean;
}

export interface ChatSessionDeps {
  reloadHistory: () => Promise<void>;
  // The first durable turn creates the routable chat ID; notify navigation only
  // once that identity exists.
  onChatStarted: (chatId: string) => void;
  focusComposer: () => void;
  setToast: ToastSetter;
  pubmedEnabled: boolean;
  webSearchEnabled: boolean;
}

// Freeze the whole completing turn across start, including its closing text,
// reasoning and fallback provenance.
export type ExecuteStartDeps = HandlerDeps & {stageToStart: SpecStage};

export interface HandlerDeps {
  state: SessionState;
  update: Dispatch<SessionUpdate>;
  services: ChatSessionDeps;
  turnAbortRef: RefObject<AbortController | null>;
}
