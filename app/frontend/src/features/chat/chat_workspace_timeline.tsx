import {type RunFocus, type RunTier} from '@/shared/api/runs';
import {type Dispatch, type ReactNode, type SetStateAction} from 'react';
import {type NavigateFunction} from 'react-router-dom';
import {type LinkedDraftRecovery, type SpecStage} from './use_chat_session';
import {type InferredRunSpec} from '@/shared/lib/run_spec';
import {
  type ChatEntry,
  AssistantMessage,
  ChatBubble,
} from './chat_timeline_bubble';
import {
  type StartedSession,
  RunSpecCard,
  StartedSessionCard,
} from './chat_timeline_run_spec_card';
import {runPath} from '@/shared/lib/routes';
import {SettledReply} from '@/shared/ui/settled_reply';

export interface TimelineItem {
  id: string;
  at: number;
  order: number;
  node: ReactNode;
  // Revision makes in-place streamed growth visible to scrolling despite
  // unchanged item id and timestamp.
  revision?: string | number;
}

export const DRAFT_SPEC_ITEM_ID = 'draft-spec';
const CONFIRMED_SPEC_ITEM_ID = 'confirmed-spec';

export interface BuildTimelineItemsArgs {
  messages: ChatEntry[];
  handleEditMessage: (message: ChatEntry, content: string) => void;
  handleCopyRequest: (message: ChatEntry) => Promise<void>;
  handleRetryMessage: (message: ChatEntry) => void;
  draft: SpecStage | null;
  setDraft: Dispatch<SetStateAction<SpecStage | null>>;
  isStarting: boolean;
  isAwaitingAgent: boolean;
  agentReasoning: string;
  agentDraft: string;
  handleCancelDraftSpec: () => void;
  handleRetryDraftSpec: () => void;
  handleStartRun: () => Promise<void>;
  confirmed: SpecStage | null;
  linkedDraftRecovery: LinkedDraftRecovery;
  stageDraftSpec: (spec: InferredRunSpec, createdAt?: number) => void;
  startedSession: StartedSession | null;
  navigate: NavigateFunction;
  resetWorkspace: () => void;
  focusComposer: () => void;
}

// Bubbles mix server and client clocks; array order is authoritative, so
// times are clamped to never run backwards.
function monotonicMessageTimes(messages: ChatEntry[]): number[] {
  let latest = -Infinity;
  return messages.map(message => {
    latest = Math.max(latest, message.created_at);
    return latest;
  });
}

function latestMessageTime(messages: ChatEntry[]): number {
  const ats = monotonicMessageTimes(messages);
  return ats.length ? ats[ats.length - 1] : -Infinity;
}

// Draft/confirmed plans carry the interview's closing reply rather than a
// separate bubble. Choose the latest durable prose; never use stream drafts
// or reasoning. A fixed-id region survives transcript snapshot replacement.
function settledReplyProps(args: BuildTimelineItemsArgs) {
  const assistant = [...args.messages]
    .reverse()
    .find(message => message.role === 'assistant');
  const started = args.startedSession;
  const stage = args.draft ?? args.confirmed;
  // Durable Q&A follows the started card even when server and client clocks
  // disagree. Transcript order, not timestamps, determines the latest reply.
  const reply =
    assistant && assistant.turnId === undefined
      ? assistant.content
      : started
        ? started.announcing
          ? ''
          : `Research started. ${started.intro ?? ''}`.trim()
        : (stage?.intro ?? assistant?.content ?? '');
  const revision =
    assistant && assistant.turnId === undefined
      ? `qa:${assistant.messageId}`
      : started
        ? `started:${started.id}`
        : stage
          ? `plan:${stage.turnId ?? stage.createdAt}`
          : assistant
            ? `message:${assistant.id}`
            : '';
  return {
    busy:
      args.isAwaitingAgent || args.isStarting || Boolean(started?.announcing),
    reply,
    revision: `${revision}:${reply}`,
  };
}

// Setup turns lock after start; durable Q&A turns rewind their own transcript.
function messageTimelineItems(args: BuildTimelineItemsArgs): TimelineItem[] {
  const {
    messages,
    handleEditMessage,
    handleCopyRequest,
    handleRetryMessage,
    isAwaitingAgent,
    startedSession,
  } = args;
  const revisable = !isAwaitingAgent;
  const ats = monotonicMessageTimes(messages);
  return messages.map((message, index) => ({
    id: `local-message-${message.id}`,
    at: ats[index],
    order: index,
    node: (
      <ChatBubble
        message={message}
        revisable={
          revisable &&
          (startedSession
            ? message.messageId !== undefined
            : message.turnId !== undefined)
        }
        onSubmitEdit={content => handleEditMessage(message, content)}
        onCopyRequest={() => void handleCopyRequest(message)}
        onRetry={() => handleRetryMessage(message)}
      />
    ),
  }));
}

// Keep reasoning and prose in the same row so settlement cannot change spacing
// or their relative position.
function thinkingTimelineItems({
  messages,
  startedSession,
  isAwaitingAgent,
  agentReasoning,
  agentDraft,
}: Pick<
  BuildTimelineItemsArgs,
  | 'messages'
  | 'startedSession'
  | 'isAwaitingAgent'
  | 'agentReasoning'
  | 'agentDraft'
>): TimelineItem[] {
  if (startedSession || !isAwaitingAgent) return [];
  return [
    {
      id: 'agent-turn-in-flight',
      at: Math.max(Date.now() / 1000, latestMessageTime(messages)),
      order: 45,
      revision: agentDraft.length + agentReasoning.length,
      node: (
        <AssistantMessage
          content={agentDraft}
          reasoning={agentReasoning}
          live
          streaming
        />
      ),
    },
  ];
}

function qaAnswerTimelineItems({
  messages,
  startedSession,
  isAwaitingAgent,
  agentReasoning,
  agentDraft,
}: Pick<
  BuildTimelineItemsArgs,
  | 'messages'
  | 'startedSession'
  | 'isAwaitingAgent'
  | 'agentReasoning'
  | 'agentDraft'
>): TimelineItem[] {
  if (!startedSession || !isAwaitingAgent) return [];
  if (!agentDraft && !agentReasoning) return [];
  return [
    {
      id: 'qa-answer-draft',
      at: Math.max(Date.now() / 1000, latestMessageTime(messages)),
      order: 46,
      revision: agentDraft.length + agentReasoning.length,
      node: (
        <AssistantMessage
          content={agentDraft}
          reasoning={agentReasoning}
          live
          streaming
        />
      ),
    },
  ];
}

function updateDraftSpec(
  setDraft: Dispatch<SetStateAction<SpecStage | null>>,
  patch: Partial<InferredRunSpec>,
): void {
  setDraft(current =>
    current ? {...current, spec: {...current.spec, ...patch}} : current,
  );
}

type DraftSpecCardArgs = Omit<
  Pick<
    BuildTimelineItemsArgs,
    | 'draft'
    | 'isStarting'
    | 'setDraft'
    | 'handleCancelDraftSpec'
    | 'handleRetryDraftSpec'
    | 'handleStartRun'
  >,
  'draft'
> & {draft: SpecStage};

function draftSpecCardNode({
  draft,
  isStarting,
  setDraft,
  handleCancelDraftSpec,
  handleRetryDraftSpec,
  handleStartRun,
}: DraftSpecCardArgs): ReactNode {
  return (
    <RunSpecCard
      spec={draft.spec}
      anchorId={DRAFT_SPEC_ITEM_ID}
      isStarting={isStarting}
      intro={draft.intro}
      introReasoning={draft.reasoning}
      introFallback={draft.fallback}
      onFocusChange={(focus: RunFocus) => updateDraftSpec(setDraft, {focus})}
      onTierChange={(tier: RunTier) => updateDraftSpec(setDraft, {tier})}
      onFieldsChange={patch => updateDraftSpec(setDraft, patch)}
      onCancel={handleCancelDraftSpec}
      onRetry={() => handleRetryDraftSpec()}
      onStart={() => void handleStartRun()}
    />
  );
}

function draftTimelineItems({
  draft,
  isStarting,
  setDraft,
  handleCancelDraftSpec,
  handleRetryDraftSpec,
  handleStartRun,
}: Pick<
  BuildTimelineItemsArgs,
  | 'draft'
  | 'isStarting'
  | 'setDraft'
  | 'handleCancelDraftSpec'
  | 'handleRetryDraftSpec'
  | 'handleStartRun'
>): TimelineItem[] {
  if (!draft) return [];
  return [
    {
      id: DRAFT_SPEC_ITEM_ID,
      at: draft.createdAt,
      order: 50,
      node: draftSpecCardNode({
        draft,
        isStarting,
        setDraft,
        handleCancelDraftSpec,
        handleRetryDraftSpec,
        handleStartRun,
      }),
    },
  ];
}

// Confirmed plans retain the same closing reply, reasoning and fallback
// provenance as their draft.
function confirmedSpecTimelineItems({
  confirmed,
  linkedDraftRecovery,
  handleStartRun,
  isStarting,
  stageDraftSpec,
}: Pick<
  BuildTimelineItemsArgs,
  | 'confirmed'
  | 'linkedDraftRecovery'
  | 'handleStartRun'
  | 'isStarting'
  | 'stageDraftSpec'
>): TimelineItem[] {
  if (!confirmed) return [];
  const {canContinueLinkedDraft, spec, status, retryStatusLookup} =
    linkedDraftRecovery;
  return [
    {
      id: CONFIRMED_SPEC_ITEM_ID,
      at: confirmed.createdAt,
      order: 50,
      node: (
        <RunSpecCard
          spec={canContinueLinkedDraft && spec ? spec : confirmed.spec}
          isStarting={isStarting}
          recoveryAction={canContinueLinkedDraft}
          recoveryLookupStatus={status}
          onRetryStatusLookup={retryStatusLookup}
          intro={confirmed.intro}
          introReasoning={confirmed.reasoning}
          introFallback={confirmed.fallback}
          locked
          onFocusChange={() => undefined}
          onTierChange={() => undefined}
          onFieldsChange={() => undefined}
          onCancel={() => undefined}
          onRetry={() => {
            stageDraftSpec(confirmed.spec);
          }}
          onStart={
            canContinueLinkedDraft
              ? () => void handleStartRun()
              : () => undefined
          }
        />
      ),
    },
  ];
}

// Include reasoning, reply and announcing state in the signature: each can
// grow the same fixed-id card.
function startedCardRevision(session: StartedSession): string {
  const intro = session.intro?.length ?? 0;
  const reasoning = session.reasoning?.length ?? 0;
  return `${intro}:${reasoning}:${Boolean(session.announcing)}`;
}

// The card's own time comes from a different clock than the bubbles, so it
// sits directly after the start request whenever one is present.
function startedCardPosition(
  messages: ChatEntry[],
  session: StartedSession,
): {at: number; order: number} {
  const index = messages.findIndex(message => message.startRequest);
  if (index < 0) return {at: session.at, order: 60};
  return {at: monotonicMessageTimes(messages)[index], order: index + 0.5};
}

function startedTimelineItems({
  messages,
  startedSession,
  navigate,
  resetWorkspace,
  focusComposer,
}: Pick<
  BuildTimelineItemsArgs,
  | 'messages'
  | 'startedSession'
  | 'navigate'
  | 'resetWorkspace'
  | 'focusComposer'
>): TimelineItem[] {
  if (!startedSession) return [];
  return [
    {
      id: `started-session-${startedSession.id}`,
      ...startedCardPosition(messages, startedSession),
      revision: startedCardRevision(startedSession),
      node: (
        <StartedSessionCard
          session={startedSession}
          href={runPath(startedSession.id, 'details')}
          onNewTopic={() => {
            // Leave the old chat route when clearing; otherwise history
            // rehydration immediately reattaches its started run.
            resetWorkspace();
            void navigate('/');
            focusComposer();
          }}
        />
      ),
    },
  ];
}

export function buildTimelineItems(
  args: BuildTimelineItemsArgs,
): TimelineItem[] {
  const timelineItems: TimelineItem[] = [
    ...messageTimelineItems(args),
    ...thinkingTimelineItems(args),
    ...qaAnswerTimelineItems(args),
    ...draftTimelineItems(args),
    ...confirmedSpecTimelineItems(args),
    ...startedTimelineItems(args),
  ];
  timelineItems.sort((a, b) => a.at - b.at || a.order - b.order);
  if (!timelineItems.length) return timelineItems;
  return [
    {
      id: 'settled-reply',
      at: -Infinity,
      order: -1,
      node: <SettledReply {...settledReplyProps(args)} />,
    },
    ...timelineItems,
  ];
}
