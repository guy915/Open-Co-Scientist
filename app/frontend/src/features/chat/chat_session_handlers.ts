import {
  type Interview,
  type InterviewSinks,
  type QaSource,
  type StagedDocument,
  addInterviewTurn,
  askRunQuestion,
  createInterview,
  editInterviewTurn,
  getInterview,
  getRunMessages,
  retryInterviewTurn,
  stageDocument,
} from '@/shared/api/runs';
import {copyText} from '@/shared/lib/clipboard';
import type {FormEvent} from 'react';
import type {ChatEntry} from './chat_timeline_bubble';
import {promoteDraftToRun} from './chat_session_start_run';
import {
  appendChatMessage,
  applyInterview,
  beginTurnAbort,
  emitDiagnosticEvent,
  isAbortError,
  qaMessagesToEntries,
} from './chat_session_transcript';
import {announceChatsChanged} from '@/shared/hooks/history_context';
import {type HandlerDeps, clearedLifecycle} from './use_chat_session';
import {errorMessage} from '@/shared/lib/errors';

type SubmitComposerDeps = HandlerDeps & {
  files: File[];
  // Choice clicks must preserve a half-written composer message.
  answer?: string;
};

function beginComposerTurn(deps: SubmitComposerDeps): string | null {
  const text = (deps.answer ?? deps.state.input).trim();
  if (!text) return null;
  deps.update({...(deps.answer === undefined ? {input: ''} : {}), error: null});
  deps.services.setToast(null);
  return text;
}

// Stage attachments before this interview turn so the Agent can read them now;
// reuse those document IDs when creating the run.
async function stageTurnFiles(
  deps: SubmitComposerDeps,
): Promise<StagedDocument[]> {
  if (!deps.files.length) return [];
  const staged = await Promise.all(deps.files.map(file => stageDocument(file)));
  deps.update(current => ({
    pendingAttachments: [...current.pendingAttachments, ...staged],
  }));
  return staged;
}

function startInterviewTurn(
  deps: Pick<HandlerDeps, 'state'>,
  text: string,
  sinks: InterviewSinks,
  documentIds: string[],
  signal: AbortSignal,
): Promise<Interview> {
  if (deps.state.interview) {
    return addInterviewTurn(
      deps.state.interview.id,
      text,
      sinks,
      documentIds,
      signal,
    );
  }
  return createInterview(text, sinks, documentIds, signal);
}

// A scientist-requested stop is not a provider/network failure and must not show
// the failure banner.
async function handleSubmitOutcome(
  deps: SubmitComposerDeps,
  error: unknown,
  isFirstTurn: boolean,
  text: string,
): Promise<void> {
  if (!isAbortError(error)) {
    deps.update({error: describeSubmitError(error)});
    return;
  }
  if (isFirstTurn) {
    // Creation reveals its ID only in the closing frame; after abort restore
    // composer text because there is no known chat ID to resync.
    clearConversation(deps, text);
    await deps.services.reloadHistory();
    return;
  }
  await recoverFromStoppedTurn(deps, deps.state.interview?.id);
}

// Only the durable model response derives runnable setup; browser keyword
// guesses must not complete an interview.
async function submitComposerMessage(deps: SubmitComposerDeps): Promise<void> {
  // Run creation closes the interview; this guard also protects callers outside
  // the handler router from posting later turns.
  if (deps.state.startedSession) return;
  const text = beginComposerTurn(deps);
  if (text === null) return;

  // The optimistic prompt is replaced by its durable turn, including the ID
  // needed for later revisions.
  appendChatMessage(deps.update, {role: 'user', content: text});
  deps.update({
    isStarting: true,
    isAwaitingAgent: true,
    agentReasoning: '',
    agentDraft: '',
  });
  const sinks = {
    onReasoning: (fragment: string) =>
      deps.update(current => ({
        agentReasoning: current.agentReasoning + fragment,
      })),
    onProse: (fragment: string) =>
      deps.update(current => ({agentDraft: current.agentDraft + fragment})),
  };
  const isFirstTurn = deps.state.interview === null;
  const signal = beginTurnAbort(deps);
  try {
    const staged = await stageTurnFiles(deps);
    const updated = await startInterviewTurn(
      deps,
      text,
      sinks,
      staged.map(document => document.id),
      signal,
    );
    deps.update({interview: updated});
    announceChatsChanged();
    if (isFirstTurn) deps.services.onChatStarted(updated.id);
    applyAgentTurn(updated, deps);
  } catch (error) {
    await handleSubmitOutcome(deps, error, isFirstTurn, text);
  } finally {
    deps.turnAbortRef.current = null;
    deps.update({isStarting: false});
    settleTurn(deps);
  }
}

type ClearConversationDeps = Pick<HandlerDeps, 'update'>;

function clearConversation(deps: ClearConversationDeps, input: string): void {
  deps.update({...clearedLifecycle, messages: [], error: null, input});
}

function cancelDraftSpec(
  deps: ClearConversationDeps & Pick<HandlerDeps, 'services'>,
) {
  clearConversation(deps, '');
  deps.services.setToast('The session was canceled');
  emitDiagnosticEvent({
    stage: 'LIFECYCLE',
    payload: {event: 'draft_cancelled'},
  });
}

type CopyMessagePromptDeps = Pick<HandlerDeps, 'update' | 'services'> & {
  message: ChatEntry;
};

async function copyMessagePrompt({
  message,
  update,
  services: {setToast, focusComposer},
}: CopyMessagePromptDeps): Promise<void> {
  const promptText = message.content;
  if (!(await copyText(promptText))) return;
  // Only the explicit toast action starts a new chat; retain no runtime or Files.
  setToast({
    message: 'Prompt copied',
    action: {
      label: 'Start new chat',
      onClick: () => {
        clearConversation({update}, promptText);
        setToast(null);
        focusComposer();
      },
    },
  });
  emitDiagnosticEvent({stage: 'CHAT', payload: {event: 'prompt_copied'}});
}

function stopTurn(deps: Pick<HandlerDeps, 'turnAbortRef'>): void {
  deps.turnAbortRef.current?.abort();
}

// Read live dependencies at call time; destructuring while building stable
// handlers would freeze stale state.
export function buildChatHandlers(handlerDeps: HandlerDeps) {
  return {
    handleRetryMessage: (message: ChatEntry) =>
      retryAssistantMessage(handlerDeps, message),
    handleEditMessage: (message: ChatEntry, content: string) =>
      editUserMessage(handlerDeps, message, content),
    handleCopyRequest: (message: ChatEntry) =>
      copyMessagePrompt({message, ...handlerDeps}),
    handleRetryDraftSpec: () => retryDraftSpec(handlerDeps),
    handleCancelDraftSpec: () => cancelDraftSpec(handlerDeps),
    // A started run closes its interview; read current session state at call
    // time to route later submissions to run Q&A.
    handleSubmit: (e: FormEvent<HTMLFormElement>, files: File[] = []) => {
      if (handlerDeps.state.startedSession) {
        return submitRunQuestion({e, ...handlerDeps});
      }
      // Question-choice clicks share the submit path but have no form event to
      // prevent.
      e.preventDefault();
      return submitComposerMessage({files, ...handlerDeps});
    },
    handleAnswerQuestions: (answer: string) =>
      submitComposerMessage({files: [], answer, ...handlerDeps}),
    handleStartRun: () => promoteDraftToRun(handlerDeps),
    handleStop: () => stopTurn(handlerDeps),
  };
}

// Server snapshots replace the log so invalidated revisions disappear and
// surviving bubbles retain durable turn IDs.
function applyAgentTurn(
  updated: Interview,
  deps: Pick<HandlerDeps, 'update'>,
): void {
  applyInterview(deps.update, updated);
  emitDiagnosticEvent(
    updated.status === 'completed'
      ? {
          stage: 'LIFECYCLE',
          payload: {event: 'interview_completed', interview_id: updated.id},
        }
      : {
          stage: 'CHAT',
          payload: {event: 'interview_advanced', interview_id: updated.id},
        },
  );
}

function describeSubmitError(error: unknown): string {
  return errorMessage(error, 'The Agent could not continue the interview.');
}

// Clear streamed text with its awaiting flag; otherwise starting research can
// reshow the previous interview reply as a new bubble.
function settleTurn(deps: Pick<HandlerDeps, 'update'>): void {
  deps.update({agentReasoning: '', agentDraft: '', isAwaitingAgent: false});
}

// Stopped interview streams leave no durable draft; resync committed server
// turns, handling aborted creation separately when its ID is still unknown.
async function recoverFromStoppedTurn(
  deps: Pick<HandlerDeps, 'update'>,
  interviewId: string | undefined,
): Promise<void> {
  deps.update({agentReasoning: '', agentDraft: ''});
  if (!interviewId) return;
  const updated = await getInterview(interviewId);
  applyInterview(deps.update, updated);
}

type AskComposerDeps = HandlerDeps & {e: FormEvent<HTMLFormElement>};

function beginAskTurn(deps: AskComposerDeps): string | null {
  const text = deps.state.input.trim();
  if (!text) return null;
  deps.update({input: '', error: null});
  deps.services.setToast(null);
  return text;
}

function describeAskError(error: unknown): string {
  return errorMessage(error, 'The Agent could not answer the question.');
}

function buildAskSinks(deps: Pick<HandlerDeps, 'update'>) {
  let answer = '';
  let reasoning = '';
  let sources: QaSource[] = [];
  return {
    sinks: {
      onSources: (found: QaSource[]) => {
        sources = found;
      },
      onReasoning: (fragment: string) => {
        reasoning += fragment;
        deps.update(current => ({
          agentReasoning: current.agentReasoning + fragment,
        }));
      },
      onChunk: (fragment: string) => {
        answer += fragment;
        deps.update(current => ({agentDraft: current.agentDraft + fragment}));
      },
    },
    result: () => ({answer, reasoning, sources}),
  };
}

// Refresh durable Q&A IDs after completion so fresh turns can be revised.
async function runAskRequest(
  deps: HandlerDeps,
  runId: string,
  text: string,
  revisionId?: number,
): Promise<void> {
  if (revisionId === undefined)
    appendChatMessage(deps.update, {role: 'user', content: text});
  // Block overlapping sends or one turn loses its Stop controller while both
  // streams corrupt the same draft.
  deps.update({
    isStarting: true,
    isAwaitingAgent: true,
    agentReasoning: '',
    agentDraft: '',
  });
  const {sinks, result} = buildAskSinks(deps);
  const signal = beginTurnAbort(deps);
  try {
    if (revisionId === undefined)
      await askRunQuestion(runId, text, sinks, signal);
    else await askRunQuestion(runId, text, sinks, signal, revisionId);
    const {answer, reasoning, sources} = result();
    appendChatMessage(deps.update, {
      role: 'assistant',
      content: answer,
      reasoning: reasoning || undefined,
      sources,
    });
  } catch (error) {
    if (!isAbortError(error)) deps.update({error: describeAskError(error)});
  } finally {
    try {
      const rows = await getRunMessages(runId);
      if (rows.length)
        deps.update(current => ({
          messages: [
            ...current.messages.filter(entry => entry.turnId !== undefined),
            ...qaMessagesToEntries(rows),
          ],
        }));
    } catch {
      // Keep the visible response if the follow-up read is unavailable.
    }
    deps.turnAbortRef.current = null;
    deps.update({isStarting: false});
    settleTurn(deps);
  }
}

async function submitRunQuestion(deps: AskComposerDeps): Promise<void> {
  deps.e.preventDefault();
  const runId = deps.state.startedSession?.id;
  if (!runId) return;
  const text = beginAskTurn(deps);
  if (text === null) return;
  await runAskRequest(deps, runId, text);
}

// Edits and retries rewind invalidated downstream turns; adopt the replacement
// transcript rather than appending a composer turn.
async function reviseInterviewTurn(
  deps: HandlerDeps,
  revise: (sinks: InterviewSinks, signal: AbortSignal) => Promise<Interview>,
): Promise<void> {
  deps.update({
    error: null,
    isAwaitingAgent: true,
    agentReasoning: '',
    agentDraft: '',
  });
  deps.services.setToast(null);
  const signal = beginTurnAbort(deps);
  try {
    const updated = await revise(
      {
        onReasoning: fragment =>
          deps.update(current => ({
            agentReasoning: current.agentReasoning + fragment,
          })),
        onProse: fragment =>
          deps.update(current => ({agentDraft: current.agentDraft + fragment})),
      },
      signal,
    );
    applyAgentTurn(updated, deps);
    announceChatsChanged();
  } catch (error) {
    if (isAbortError(error)) {
      await recoverFromStoppedTurn(deps, deps.state.interview?.id);
    } else {
      deps.update({error: describeSubmitError(error)});
    }
  } finally {
    deps.turnAbortRef.current = null;
    settleTurn(deps);
  }
}

// Optimistic bubbles have no durable revision target; started runs lock the
// transcript they consumed.
function revisableTurn(
  deps: HandlerDeps,
  message: ChatEntry,
): {interviewId: string; turnId: number} | null {
  if (deps.state.startedSession) return null;
  const interviewId = deps.state.interview?.id;
  if (!interviewId || !message.turnId) return null;
  return {interviewId, turnId: message.turnId};
}

function truncateAtMessage(
  messages: ChatEntry[],
  target: ChatEntry,
  content?: string,
): ChatEntry[] {
  const index = messages.findIndex(entry => entry.id === target.id);
  if (index < 0) return messages;
  const kept = messages.slice(0, index);
  return content === undefined ? kept : [...kept, {...target, content}];
}

function editUserMessage(
  deps: HandlerDeps,
  message: ChatEntry,
  content: string,
): void {
  const text = content.trim();
  if (deps.state.isAwaitingAgent || !text) return;
  if (deps.state.startedSession && message.messageId !== undefined) {
    void runAskRequest(
      deps,
      deps.state.startedSession.id,
      text,
      message.messageId,
    );
    return;
  }
  const target = revisableTurn(deps, message);
  if (!target || !text) return;
  deps.update(current => ({
    messages: truncateAtMessage(current.messages, message, text),
  }));
  void reviseInterviewTurn(deps, (sinks, signal) =>
    editInterviewTurn(target.interviewId, target.turnId, text, sinks, signal),
  );
  emitDiagnosticEvent({stage: 'CHAT', payload: {event: 'prompt_edited'}});
}

function retryAssistantMessage(deps: HandlerDeps, message: ChatEntry): void {
  if (deps.state.isAwaitingAgent) return;
  if (deps.state.startedSession && message.messageId !== undefined) {
    void runAskRequest(
      deps,
      deps.state.startedSession.id,
      '',
      message.messageId,
    );
    return;
  }
  const target = revisableTurn(deps, message);
  if (!target) return;
  deps.update(current => ({
    messages: truncateAtMessage(current.messages, message),
  }));
  void reviseInterviewTurn(deps, (sinks, signal) =>
    retryInterviewTurn(target.interviewId, target.turnId, sinks, signal),
  );
  emitDiagnosticEvent({stage: 'CHAT', payload: {event: 'response_retried'}});
}

function draftRevisionTarget(deps: HandlerDeps): {
  interviewId: string;
  turnId: number;
} | null {
  const interviewId = deps.state.interview?.id;
  const turnId = deps.state.draft?.turnId;
  if (!interviewId || !turnId) return null;
  return {interviewId, turnId};
}

// A plan is its closing Agent turn; retry that turn rather than re-staging the
// unchanged spec.
function retryDraftSpec(deps: HandlerDeps): void {
  // Rehydration may leave a draft beside a started session; draft presence alone
  // cannot authorize revision.
  if (deps.state.startedSession) return;
  const target = draftRevisionTarget(deps);
  if (!target) return;
  deps.update({draft: null});
  void reviseInterviewTurn(deps, (sinks, signal) =>
    retryInterviewTurn(target.interviewId, target.turnId, sinks, signal),
  );
  emitDiagnosticEvent({stage: 'CHAT', payload: {event: 'plan_retried'}});
}
