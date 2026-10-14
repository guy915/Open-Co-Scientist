import {
  appendChatMessage,
  emitDiagnosticEvent,
} from './chat_session_transcript';
import {
  announceRunStart,
  isDraftStatus,
  isFailureStatus,
  cancelRun,
  createRun,
  getRun,
  isCancelledStatus,
  isStartedStatus,
  retiresStartIntent,
  startRun,
  type Run,
} from '@/shared/api/runs';
import {displayTitle} from '@/shared/lib/titles';
import {RUNS_CHANGED_EVENT} from '@/shared/lib/dom_events';
import type {StartedSession} from './chat_timeline_run_spec_card';
import {announceChatsChanged} from '@/shared/hooks/history_context';
import {
  type ExecuteStartDeps,
  type HandlerDeps,
  type SpecStage,
  type SessionState,
} from './use_chat_session';
import {beginTurnAbort, isAbortError} from './chat_session_transcript';
import {interviewToRunSpec} from '@/shared/lib/run_spec';
import {
  resolveByokRoutes,
  getClientId,
  makePrefixedId,
} from '@/shared/lib/client_id';
import {
  recoverySpecForRun,
  type LinkedRunTarget,
  type PendingRunCreatePayload,
} from '@/shared/lib/run_spec';
import {readStorage, STORAGE_KEYS} from '@/shared/lib/safe_storage';
import {errorMessage, httpStatus} from '@/shared/lib/errors';
import {nowSeconds} from '@/shared/lib/time';

const START_RESEARCH_PROMPT = 'Start research';

interface StartResult {
  session: StartedSession;
  shouldAnnounce: boolean;
}

type StartDeps = StartRecoveryDeps;

function validateStartTarget(
  target: ResolvedStartTarget,
  deps: Pick<ExecuteStartDeps, 'update' | 'stageToStart'>,
): ResolvedStartTarget {
  if (isFailureStatus(target.status)) {
    deps.update({confirmed: null, draft: deps.stageToStart});
    throw runOutcomeError(target.status);
  }
  return target;
}

async function executeStart(deps: StartDeps): Promise<StartResult> {
  const chatId = deps.stageToStart.spec.interviewId;
  const target = validateStartTarget(await resolveStartTarget(deps), deps);
  const stage = target.stage ?? deps.stageToStart;
  let shouldAnnounce = isDraftStatus(target.status);

  if (shouldAnnounce) {
    // The plan card carries a server time; a lagging client clock must not
    // lift this bubble above it.
    appendChatMessage(deps.update, {
      role: 'user',
      content: START_RESEARCH_PROMPT,
      createdAt: Math.max(nowSeconds(), stage.createdAt),
      startRequest: true,
    });
  }
  deps.update({confirmed: stage, draft: null});
  if (shouldAnnounce) {
    const disposition = await startOrSettle(
      target.runId,
      chatId,
      target.intent,
      deps,
    );
    shouldAnnounce = disposition === 'started';
  }
  const session: StartedSession = {
    id: target.runId,
    title: displayTitle(null, stage.spec.goal),
    at: nowSeconds(),
    announcing: shouldAnnounce,
  };
  deps.update({pendingAttachments: [], input: '', startedSession: session});
  await deps.services.reloadHistory();
  window.dispatchEvent(new Event(RUNS_CHANGED_EVENT));
  announceChatsChanged();
  return {session, shouldAnnounce};
}

function appendAnnouncement(
  update: HandlerDeps['update'],
  patch: 'intro' | 'reasoning',
  fragment: string,
): void {
  update(({startedSession}) => ({
    startedSession: startedSession
      ? {...startedSession, [patch]: (startedSession[patch] ?? '') + fragment}
      : startedSession,
  }));
}

async function announceStart(
  deps: ExecuteStartDeps & Pick<HandlerDeps, 'turnAbortRef'>,
  runId: string,
): Promise<void> {
  deps.update(({startedSession}) => ({
    startedSession: startedSession
      ? {...startedSession, announcing: true}
      : startedSession,
  }));
  // Expose announcement cancellation to Stop so a hanging provider cannot leave
  // the composer blocked.
  deps.update({isAwaitingAgent: true});
  try {
    const outcome = await announceRunStart(
      runId,
      START_RESEARCH_PROMPT,
      {
        onReasoning: fragment =>
          appendAnnouncement(deps.update, 'reasoning', fragment),
        onChunk: fragment => appendAnnouncement(deps.update, 'intro', fragment),
      },
      beginTurnAbort(deps),
    );
    emitDiagnosticEvent({
      stage: 'CHAT',
      runId,
      payload: {
        event: 'start_announced',
        fallback: Boolean(outcome?.fallback),
      },
    });
  } catch (error) {
    const stopped = isAbortError(error);
    emitDiagnosticEvent({
      stage: 'CHAT',
      runId,
      payload: {
        event: stopped
          ? 'start_announcement_stopped'
          : 'start_announcement_failed',
      },
    });
    // An interrupted announcement persists nothing; drop partial text so this
    // card matches its standby copy after reload.
    deps.update(({startedSession}) => ({
      startedSession: startedSession
        ? {...startedSession, intro: undefined, reasoning: undefined}
        : startedSession,
    }));
  } finally {
    deps.turnAbortRef.current = null;
    deps.update(({startedSession}) => ({
      startedSession: startedSession
        ? {...startedSession, announcing: false}
        : startedSession,
      isAwaitingAgent: false,
    }));
  }
}

async function startDraftRun(deps: StartDeps): Promise<void> {
  emitDiagnosticEvent({
    stage: 'LIFECYCLE',
    payload: {event: 'start_requested'},
  });
  try {
    const outcome = await executeStart(deps);
    const {session} = outcome;
    emitDiagnosticEvent({
      stage: 'LIFECYCLE',
      runId: session.id,
      payload: {event: 'start_queued', run_id: session.id},
    });
    // Keep announcement in the start/busy window so run Q&A cannot concurrently
    // write the same session state; Stop still reaches its controller.
    if (outcome.shouldAnnounce) await announceStart(deps, session.id);
  } catch (err) {
    const message = errorMessage(err);
    deps.update({error: message});
    emitDiagnosticEvent({
      stage: 'LIFECYCLE',
      level: 'error',
      payload: {event: 'start_failed', message},
    });
  }
}

export async function promoteDraftToRun(deps: HandlerDeps): Promise<void> {
  const stageToStart =
    deps.state.draft ?? linkedInterviewStage(deps.state.interview);
  if (!stageToStart || deps.state.startedSession) return;
  // Snapshot the whole stage before awaits so state changes cannot replace the
  // plan being started.
  deps.update({isStarting: true, error: null});
  deps.services.setToast(null);
  try {
    await startDraftRun({...deps, stageToStart});
  } finally {
    deps.update({isStarting: false});
  }
}

function linkedInterviewStage(
  interview: SessionState['interview'],
): SessionState['draft'] {
  if (!interview?.run_id) return null;
  const closing = completedInterviewClosing(interview);
  return closing ? stageFromClosing(interview, closing) : null;
}

function completedInterviewClosing(
  interview: NonNullable<SessionState['interview']>,
) {
  if (interview.status !== 'completed') return null;
  const closing = interview.turns.at(-1);
  return closing?.role === 'agent' ? closing : null;
}

function stageFromClosing(
  interview: NonNullable<SessionState['interview']>,
  closing: NonNullable<ReturnType<typeof completedInterviewClosing>>,
): NonNullable<SessionState['draft']> {
  return {
    spec: interviewToRunSpec(interview),
    createdAt: closing.created_at,
    intro: closing.content,
    reasoning: closing.reasoning ?? undefined,
    turnId: closing.id,
    fallback: closing.fallback || undefined,
  };
}

const STORAGE_PREFIX = STORAGE_KEYS.pendingRunCreatePrefix;

interface StoredIntent {
  version: 1;
  key: string;
  ownerFingerprint: string;
  byokFingerprint: string;
  payloadJson: string;
  createdRunId?: string;
}

export interface PendingCreateIntent<
  T extends Record<string, unknown> = Record<string, unknown>,
> {
  key: string;
  payload: T;
  createdRunId?: string;
}

// Retries reuse the exact owner- and credential-scoped request rather than
// rebuilding intent from changed browser state.
export async function getPendingCreateIntent<T extends Record<string, unknown>>(
  chatId: string,
  payload: T,
): Promise<{key: string; payload: T; createdRunId?: string}> {
  const storageKey = `${STORAGE_PREFIX}${encodeURIComponent(chatId)}`;
  const payloadJson = JSON.stringify(payload);
  const [ownerFingerprint, byokFingerprint] = await Promise.all([
    fingerprint(ownerMaterial()),
    fingerprint(credentialMaterial()),
  ]);

  const stored = readIntent(storageKey);
  if (matchesIntent(stored, ownerFingerprint, byokFingerprint, payloadJson)) {
    return {
      key: stored.key,
      payload: JSON.parse(stored.payloadJson) as T,
      createdRunId: stored.createdRunId,
    };
  }

  const intent: StoredIntent = {
    version: 1,
    key: makePrefixedId('run-create'),
    ownerFingerprint,
    byokFingerprint,
    payloadJson,
  };
  sessionStorage.setItem(storageKey, JSON.stringify(intent));
  return {key: intent.key, payload: JSON.parse(payloadJson) as T};
}

export async function readPendingCreateIntent<
  T extends Record<string, unknown> = Record<string, unknown>,
>(chatId: string): Promise<PendingCreateIntent<T> | undefined> {
  const [ownerFingerprint, byokFingerprint] = await Promise.all([
    fingerprint(ownerMaterial()),
    fingerprint(credentialMaterial()),
  ]);
  const intent = readIntent(intentStorageKey(chatId));
  if (!intent || !matchesOwner(intent, ownerFingerprint, byokFingerprint)) {
    return undefined;
  }

  return publicIntent<T>(intent);
}

function matchesOwner(
  intent: StoredIntent,
  ownerFingerprint: string,
  byokFingerprint: string,
): boolean {
  return (
    intent.ownerFingerprint === ownerFingerprint &&
    intent.byokFingerprint === byokFingerprint
  );
}

function publicIntent<T extends Record<string, unknown>>(
  intent: StoredIntent,
): PendingCreateIntent<T> | undefined {
  try {
    const payload: unknown = JSON.parse(intent.payloadJson);
    if (!isRecord(payload) || Array.isArray(payload)) return undefined;
    return {
      key: intent.key,
      payload: payload as T,
      createdRunId: intent.createdRunId,
    };
  } catch {
    return undefined;
  }
}

// Learning the run ID must not change the create request’s idempotency key.
function rememberPendingCreateRun(
  chatId: string,
  key: string,
  runId: string,
): void {
  const storageKey = intentStorageKey(chatId);
  const intent = readIntent(storageKey);
  if (!intent || intent.key !== key) return;
  sessionStorage.setItem(
    storageKey,
    JSON.stringify({...intent, createdRunId: runId}),
  );
}

// Remove only the settled request intent; a later request for the same chat must
// survive stale cleanup.
function retirePendingCreateIntent(chatId: string, key: string): void {
  const storageKey = intentStorageKey(chatId);
  if (readIntent(storageKey)?.key === key) {
    sessionStorage.removeItem(storageKey);
  }
}

function intentStorageKey(chatId: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(chatId)}`;
}

function ownerMaterial(): string {
  return `client:${getClientId()}`;
}

// Both tiers' credentials identify the intent, exactly as the headers send them.
function credentialMaterial(): string {
  const routes = resolveByokRoutes();
  if (!routes) return '';
  return [routes.worker, routes.supervisor]
    .map(({provider, apiKey}) => `${provider}\u0000${apiKey}`)
    .join('\u0001');
}

function matchesIntent(
  intent: StoredIntent | undefined,
  ownerFingerprint: string,
  byokFingerprint: string,
  payloadJson: string,
): intent is StoredIntent {
  return Boolean(
    intent &&
    intent.ownerFingerprint === ownerFingerprint &&
    intent.byokFingerprint === byokFingerprint &&
    intent.payloadJson === payloadJson,
  );
}

function readIntent(key: string): StoredIntent | undefined {
  try {
    const parsed: unknown = JSON.parse(readStorage('session', key) ?? 'null');
    return isStoredIntent(parsed) ? parsed : undefined;
  } catch {
    // Corrupt session storage cannot authorize an intent; create a fresh one.
  }
  return undefined;
}

function isStoredIntent(value: unknown): value is StoredIntent {
  if (!isRecord(value) || value.version !== 1) return false;
  if (
    value.createdRunId !== undefined &&
    typeof value.createdRunId !== 'string'
  ) {
    return false;
  }
  const stringFields = [
    'key',
    'ownerFingerprint',
    'byokFingerprint',
    'payloadJson',
  ] as const;
  return stringFields.every(field => typeof value[field] === 'string');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

async function fingerprint(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), byte =>
    byte.toString(16).padStart(2, '0'),
  ).join('');
}

type SettleDeps = Pick<ExecuteStartDeps, 'update' | 'stageToStart'>;
type StartRecoveryDeps = ExecuteStartDeps;
type CreateRunPayload = Parameters<typeof createRun>[0];
type StartDisposition = 'started' | 'already-started';

interface ResolvedStartTarget {
  runId: string;
  status: string;
  intent?: PendingCreateIntent;
  stage?: SpecStage;
  run: Run;
}

function buildCreateRunPayload(deps: ExecuteStartDeps): CreateRunPayload {
  const spec = deps.stageToStart.spec;
  return {
    research_goal: spec.goal,
    interview_id: spec.interviewId,
    requirements: spec.requirements,
    attributes: spec.attributes,
    criteria: spec.criteria,
    focus: spec.focus,
    tier: spec.tier,
    notify_on_completion: Boolean(spec.notifyOnCompletion),
    completion_email: spec.notifyOnCompletion
      ? spec.completionEmail
      : undefined,
    enable_literature_review: deps.services.pubmedEnabled,
    enable_web_search: deps.services.webSearchEnabled,
    document_ids: deps.state.pendingAttachments.map(document => document.id),
  };
}

async function startOrSettle(
  runId: string,
  chatId: string | undefined,
  intent: PendingCreateIntent | undefined,
  deps: SettleDeps,
): Promise<StartDisposition> {
  try {
    await startRun(runId);
    retireIntent(chatId, intent);
    return 'started';
  } catch (error) {
    return resolveStartError(runId, chatId, intent, deps, error);
  }
}

async function resolveStartError(
  runId: string,
  chatId: string | undefined,
  intent: PendingCreateIntent | undefined,
  deps: SettleDeps,
  error: unknown,
): Promise<StartDisposition> {
  const status = await readRunStatus(runId);
  if (isStartedStatus(status)) {
    retireIntent(chatId, intent);
    return 'already-started';
  }
  if (isDraftStatus(status)) {
    return resolveDraftAfterFailedStart(runId, chatId, intent, deps, error);
  }
  if (isCancelledStatus(status)) retireIntent(chatId, intent);
  restoreDraft(deps);
  throw startErrorForStatus(status) ?? error;
}

async function resolveDraftAfterFailedStart(
  runId: string,
  chatId: string | undefined,
  intent: PendingCreateIntent | undefined,
  deps: SettleDeps,
  error: unknown,
): Promise<StartDisposition> {
  const status = await cancelAndReadStatus(runId);
  if (isCancelledStatus(status)) retireIntent(chatId, intent);
  if (isStartedStatus(status)) {
    retireIntent(chatId, intent);
    return 'already-started';
  }
  restoreDraft(deps);
  throw error;
}

async function cancelAndReadStatus(runId: string): Promise<string | undefined> {
  const cancellation = await cancelRun(runId).catch(() => undefined);
  if (isCancelledStatus(cancellation?.status)) return 'cancelled';
  // A lost cancel response does not prove cancellation: the run may have started
  // between requests.
  return readRunStatus(runId);
}

function retireIntent(
  chatId: string | undefined,
  intent: PendingCreateIntent | undefined,
): void {
  if (chatId && intent) retirePendingCreateIntent(chatId, intent.key);
}

async function readRunStatus(runId: string): Promise<string | undefined> {
  try {
    return (await getRun(runId)).status;
  } catch {
    return undefined;
  }
}

function restoreDraft(deps: SettleDeps): void {
  deps.update({confirmed: null, draft: deps.stageToStart});
}

function runOutcomeError(status: string): Error {
  return new Error(
    `The existing run is ${status}. Open its details to review it before retrying.`,
  );
}

function startErrorForStatus(status: string | undefined): Error | undefined {
  if (isFailureStatus(status)) return runOutcomeError(status);
}

async function readStartIntent(
  chatId: string | undefined,
): Promise<PendingCreateIntent<CreateRunPayload> | undefined> {
  if (!chatId) return undefined;
  return readPendingCreateIntent<CreateRunPayload>(chatId);
}

function linkedRunId(
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
  interview: SessionState['interview'],
): string | null | undefined {
  if (intent?.createdRunId) return intent.createdRunId;
  return interview?.run_id;
}

async function resolveStartTarget(
  deps: StartRecoveryDeps,
): Promise<ResolvedStartTarget> {
  const payload = buildCreateRunPayload(deps);
  const chatId = payload.interview_id;
  const intent = await readStartIntent(chatId);
  const linked = await getLinkedRun(
    linkedRunId(intent, deps.state.interview),
    chatId,
    intent,
    deps,
  );
  if (linked) {
    if (isCancelledStatus(linked.status)) {
      return createStartTarget(payload, chatId, undefined);
    }
    return linked;
  }
  if (intent && !sameCreatePayload(intent.payload, payload)) {
    return resolveChangedPendingIntent(payload, chatId, intent, deps);
  }
  return createStartTarget(payload, chatId, intent);
}

async function resolveChangedPendingIntent(
  payload: CreateRunPayload,
  chatId: string | undefined,
  intent: PendingCreateIntent<CreateRunPayload>,
  deps: StartRecoveryDeps,
): Promise<ResolvedStartTarget> {
  const previous = await postCreate(payload, chatId, intent);
  const settled = await cancelUnstartedDraft(previous, deps);
  if (isCancelledStatus(settled.status)) {
    return createAfterCancelledReceipt(payload, chatId, settled);
  }
  if (isStartedStatus(settled.status)) {
    return {
      ...settled,
      stage: stageForRun(deps.stageToStart, deps, settled.run, intent),
    };
  }
  restoreDraft(deps);
  throw startErrorForStatus(settled.status) ?? runOutcomeError(settled.status);
}

async function cancelUnstartedDraft(
  target: ResolvedStartTarget,
  deps: StartRecoveryDeps,
): Promise<ResolvedStartTarget> {
  if (!isDraftStatus(target.status)) return target;
  const status = await cancelAndReadStatus(target.runId);
  if (retiresStartIntent(status)) {
    return {...target, status};
  }
  restoreDraft(deps);
  throw new Error(
    'The previous setup is still unresolved. Retry after confirming its run was cancelled.',
  );
}

function sameCreatePayload(
  first: CreateRunPayload,
  second: CreateRunPayload,
): boolean {
  return JSON.stringify(first) === JSON.stringify(second);
}

function stageForRun(
  stage: SpecStage,
  deps: StartRecoveryDeps,
  run: Run,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
): SpecStage {
  const linked: LinkedRunTarget = {
    chatId: recoveryChatId(deps, intent),
    runId: run.id,
    chat: undefined,
    interview: deps.state.interview,
  };
  return {
    ...stage,
    spec: recoverySpecForRun(linked, run, recoveryPayload(intent)),
  };
}

function recoveryChatId(
  deps: StartRecoveryDeps,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
): string {
  return intent?.payload.interview_id ?? deps.state.interview?.id ?? '';
}

function recoveryPayload(
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
): PendingRunCreatePayload | undefined {
  return intent?.payload as PendingRunCreatePayload | undefined;
}

async function getLinkedRun(
  runId: string | null | undefined,
  chatId: string | undefined,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
  deps: StartRecoveryDeps,
): Promise<ResolvedStartTarget | undefined> {
  if (!runId) return undefined;
  intent = rememberLinkedRun(chatId, intent, runId);
  let linkedRun;
  try {
    linkedRun = await getRun(runId);
  } catch (error) {
    restoreDraft(deps);
    throw error;
  }
  if (retiresStartIntent(linkedRun.status)) retireIntent(chatId, intent);
  return {
    runId,
    status: linkedRun.status,
    intent,
    stage: stageForRun(deps.stageToStart, deps, linkedRun, intent),
    run: linkedRun,
  };
}

function rememberLinkedRun(
  chatId: string | undefined,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
  runId: string,
): PendingCreateIntent<CreateRunPayload> | undefined {
  if (!chatId || !intent || intent.createdRunId) return intent;
  rememberPendingCreateRun(chatId, intent.key, runId);
  return {...intent, createdRunId: runId};
}

async function createStartTarget(
  payload: CreateRunPayload,
  chatId: string | undefined,
  pending: PendingCreateIntent<CreateRunPayload> | undefined,
): Promise<ResolvedStartTarget> {
  let intent = pending;
  if (!intent && chatId) intent = await getPendingCreateIntent(chatId, payload);
  const target = await postCreate(payload, chatId, intent);
  if (isCancelledStatus(target.status)) {
    return createAfterCancelledReceipt(payload, chatId, target);
  }
  if (retiresStartIntent(target.status)) retireIntent(chatId, intent);
  return target;
}

async function createAfterCancelledReceipt(
  payload: CreateRunPayload,
  chatId: string | undefined,
  cancelled: ResolvedStartTarget,
): Promise<ResolvedStartTarget> {
  retireIntent(chatId, cancelled.intent);
  const intent = chatId
    ? await getPendingCreateIntent(chatId, payload)
    : undefined;
  const replacement = await postCreate(payload, chatId, intent);
  if (isCancelledStatus(replacement.status)) {
    retireIntent(chatId, replacement.intent);
    throw new Error(
      'The cancelled run was retired. Click Start research again.',
    );
  }
  if (retiresStartIntent(replacement.status)) {
    retireIntent(chatId, replacement.intent);
  }
  return replacement;
}

async function postCreate(
  payload: CreateRunPayload,
  chatId: string | undefined,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
): Promise<ResolvedStartTarget> {
  let created;
  try {
    created = await sendCreate(payload, intent);
  } catch (error) {
    retireRejectedIntent(chatId, intent, error);
    throw error;
  }
  rememberCreatedRun(chatId, intent, created.id);
  return {runId: created.id, status: created.status, intent, run: created};
}

async function sendCreate(
  payload: CreateRunPayload,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
) {
  if (intent) return createRun(intent.payload, {idempotencyKey: intent.key});
  return createRun(payload);
}

function retireRejectedIntent(
  chatId: string | undefined,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
  error: unknown,
): void {
  if (chatId && intent && isDefinitiveCreateRejection(error)) {
    retireIntent(chatId, intent);
  }
}

function rememberCreatedRun(
  chatId: string | undefined,
  intent: PendingCreateIntent<CreateRunPayload> | undefined,
  runId: string,
): void {
  if (chatId && intent) rememberPendingCreateRun(chatId, intent.key, runId);
}

function isDefinitiveCreateRejection(error: unknown): boolean {
  const status = httpStatus(error);
  return (
    status !== undefined && [400, 401, 403, 404, 413, 415, 422].includes(status)
  );
}
