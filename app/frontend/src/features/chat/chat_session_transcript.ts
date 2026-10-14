import type {QaSource} from '@/shared/api/runs';
import {
  type Interview,
  type InterviewTurn,
  type RunMessage,
} from '@/shared/api/runs';
import {makePrefixedId} from '@/shared/lib/client_id';
import type {Dispatch} from 'react';
import {emitDiagnostic} from '@/shared/lib/diagnostic_events';
import {type ChatEntry} from './chat_timeline_bubble';
import {interviewToRunSpec} from '@/shared/lib/run_spec';
import {
  type DraftIntro,
  type HandlerDeps,
  type SessionUpdate,
  type SpecStage,
  fieldSetter,
  stageDraftPatch,
} from './use_chat_session';
import {nowSeconds} from '@/shared/lib/time';

function turnToEntry(turn: InterviewTurn): ChatEntry {
  const role = turn.role === 'agent' ? 'assistant' : 'user';
  return {
    // Durable turn IDs stabilize React keys and keep expansion state through
    // snapshot rebuilds.
    id: `turn-${turn.id}`,
    role,
    content: turn.content,
    reasoning: turn.reasoning ?? undefined,
    turnId: turn.id,
    fallback: turn.fallback || undefined,
    created_at: turn.created_at,
  };
}

function splitTranscript(interview: Interview): {
  entries: ChatEntry[];
  closing: InterviewTurn | null;
} {
  const turns = [...interview.turns];
  const last = turns[turns.length - 1];
  const closing =
    interview.status === 'completed' && last?.role === 'agent' ? last : null;
  if (closing) turns.pop();
  return {entries: turns.map(turnToEntry), closing};
}

export interface TranscriptSink {
  setInterview: (interview: Interview | null) => void;
  setDraft: (stage: SpecStage | null) => void;
  setConfirmed: (stage: SpecStage | null) => void;
  stageDraftSpec: (
    spec: ReturnType<typeof interviewToRunSpec>,
    createdAt?: number,
    intro?: DraftIntro,
  ) => void;
  // Snapshot sinks accept replacement setters, not only stateful dispatch;
  // narrowing would reject valid plain-entry consumers.
  setMessages: (entries: ChatEntry[]) => void;
}

// Replace invalidated transcript turns from the server snapshot and clear
// withdrawn incomplete plans; appending would retain stale derivations.
export function applyInterview(
  sink: TranscriptSink | Dispatch<SessionUpdate>,
  interview: Interview,
): void {
  if (typeof sink === 'function') {
    const update = sink;
    sink = {
      setMessages: fieldSetter(update, 'messages'),
      setInterview: fieldSetter(update, 'interview'),
      setDraft: fieldSetter(update, 'draft'),
      setConfirmed: fieldSetter(update, 'confirmed'),
      stageDraftSpec: (spec, at, intro) =>
        update(stageDraftPatch(spec, at, intro)),
    };
  }
  const {entries, closing} = splitTranscript(interview);
  sink.setMessages(entries);
  sink.setInterview(interview);
  if (closing) {
    const intro = {
      message: closing.content,
      reasoning: closing.reasoning ?? undefined,
      turnId: closing.id,
      fallback: closing.fallback || undefined,
    };
    // Use the interview’s started-run marker to avoid a duplicate Start action
    // regardless of which independent run/chat load arrives first.
    if (interview.run_id) {
      sink.setDraft(null);
      sink.setConfirmed({
        spec: interviewToRunSpec(interview),
        createdAt: closing.created_at,
        intro: intro.message,
        reasoning: intro.reasoning,
        turnId: intro.turnId,
        fallback: intro.fallback,
      });
      return;
    }
    sink.stageDraftSpec(
      interviewToRunSpec(interview),
      closing.created_at,
      intro,
    );
    return;
  }
  sink.setDraft(null);
  sink.setConfirmed(null);
}

// Anchor the session card to the announcement timestamp, not run creation, so it
// sorts below the start request that produced it.
export interface RehydratedAnnouncement {
  intro: string;
  reasoning?: string;
  at: number;
}

function qaRole(sender: RunMessage['sender']): ChatEntry['role'] {
  return sender === 'user' ? 'user' : 'assistant';
}

function rowToEntry(row: RunMessage): ChatEntry {
  return {
    id: `qa-${row.id}`,
    messageId: row.kind === 'qa' ? row.id : undefined,
    role: qaRole(row.sender),
    content: row.content,
    reasoning: row.meta?.reasoning,
    sources: row.meta?.sources,
    created_at: row.created_at,
    startRequest: row.kind === 'start' || undefined,
  };
}

function isBubbleRow(row: RunMessage): boolean {
  if (row.kind === 'qa') return true;
  return row.kind === 'start' && row.sender === 'user';
}

export function qaMessagesToEntries(rows: RunMessage[]): ChatEntry[] {
  return rows.filter(isBubbleRow).map(rowToEntry);
}

// Use the first announcement when retries or multiple tabs produced several;
// legacy runs retain standby copy when none exists.
export function runStartAnnouncement(
  rows: RunMessage[],
): RehydratedAnnouncement | null {
  const reply = rows.find(
    row => row.kind === 'start' && row.sender === 'system',
  );
  if (!reply) return null;
  return {
    intro: reply.content,
    reasoning: reply.meta?.reasoning,
    at: reply.created_at,
  };
}

export type DiagnosticStage = 'LIFECYCLE' | 'CHAT';

export function emitDiagnosticEvent(detail: {
  stage: DiagnosticStage;
  runId?: string;
  level?: 'info' | 'warning' | 'error';
  payload?: Record<string, unknown>;
}) {
  emitDiagnostic(detail);
}

export interface NewChatMessage {
  role: 'assistant' | 'user';
  content: string;
  reasoning?: string;
  createdAt?: number;
  sources?: QaSource[];
  startRequest?: boolean;
}

export function appendChatMessage(
  update: Dispatch<SessionUpdate>,
  message: NewChatMessage,
): number {
  const createdAt = message.createdAt ?? nowSeconds();
  const entry: ChatEntry = {
    id: makePrefixedId(message.role),
    role: message.role,
    content: message.content,
    reasoning: message.reasoning,
    sources: message.sources,
    created_at: createdAt,
    startRequest: message.startRequest,
  };
  update(current => ({messages: [...current.messages, entry]}));
  return createdAt;
}

// Enumerable getters preserve call-time freshness even when handlers spread
// dependencies; the closed interface fixes the initial key set.
export function liveHandlerDeps<T extends object>(ref: {current: T}): T {
  const live = {} as T;
  for (const key of Object.keys(ref.current) as (keyof T)[]) {
    Object.defineProperty(live, key, {
      enumerable: true,
      get: () => ref.current[key],
    });
  }
  return live;
}

// DOMException need not be an Error; classify scientist-requested aborts by name
// rather than instanceof.
export function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as {name?: unknown}).name === 'AbortError'
  );
}

export function beginTurnAbort(
  deps: Pick<HandlerDeps, 'turnAbortRef'>,
): AbortSignal {
  const controller = new AbortController();
  deps.turnAbortRef.current = controller;
  return controller.signal;
}
