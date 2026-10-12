import {
  getClientId,
  getStoredApiKey,
  getStoredModel,
  resolveByokRoutes,
} from '@/shared/lib/client_id';
import type {RunFocus, RunStatus, RunTier} from './wire_common';
import type {ChatSummary, Interview} from './wire_interviews';
import type {Report} from './wire_reports';
import type {QaSource, Run, RunMessage, RunWithSummary} from './wire_runs';
import type {
  ClaimEvidenceRow,
  Evidence,
  Hypothesis,
  MatchRow,
  Review,
  SafetyDecision,
} from './wire_science';
import {emitDiagnostic} from '@/shared/lib/diagnostic_events';

export type * from './wire_common';
export type * from './wire_interviews';
export type * from './wire_reports';
export type * from './wire_runs';
export type * from './wire_science';
export type * from './wire_system';

export function runGoal(run: Run | null | undefined): string {
  if (!run) return '';
  return run.config.setup?.goal ?? run.research_goal ?? '';
}

export function createRun(
  input: {
    research_goal: string;
    interview_id?: string;
    // Creation copies staged documents atomically, so initial grounding cannot
    // fail as an independent follow-up upload.
    document_ids?: string[];
    requirements?: string[];
    attributes?: string[];
    criteria?: string[];
    focus?: RunFocus;
    tier?: RunTier;
    initial_hypotheses_count?: number;
    max_iterations?: number;
    evolution_max_count?: number;
    k_factor?: number;
    enable_literature_review?: boolean;
    enable_web_search?: boolean;
    notify_on_completion?: boolean;
    completion_email?: string;
  },
  options?: {idempotencyKey?: string},
): Promise<Run> {
  const init = jsonRequest(input, true);
  // The backend validates BYOK provider/key pairs before admitting a run;
  // credentials travel only in headers.
  return fetchJson('/api/runs', {
    ...init,
    headers: {
      ...init.headers,
      ...byokHeaders(),
      ...(options?.idempotencyKey
        ? {'Idempotency-Key': options.idempotencyKey}
        : {}),
    },
  });
}

export function listRuns(limit?: number): Promise<Run[]> {
  const query = limit === undefined ? '' : `?limit=${limit}`;
  return fetchField(`/api/runs${query}`, 'runs', {headers: clientHeaders()});
}

export function listDemoRuns(): Promise<Run[]> {
  return fetchField('/api/runs/demo', 'runs');
}

// A failing owned/demo source must not prevent loading the other history.
export async function loadRunHistory(): Promise<Run[]> {
  const [ownedRuns, demoRuns] = await Promise.all([
    listRuns().catch(() => [] as Run[]),
    listDemoRuns().catch(() => [] as Run[]),
  ]);
  const byId = new Map([...ownedRuns, ...demoRuns].map(run => [run.id, run]));
  return [...byId.values()].sort((a, b) => b.updated_at - a.updated_at);
}

export function getRun(id: string): Promise<RunWithSummary> {
  return fetchJson(`/api/runs/${id}`, {headers: clientHeaders()});
}

export function resumeRun(id: string): Promise<{id: string; status: string}> {
  return fetchJson(
    `/api/runs/${id}/resume`,
    jsonRequest({acknowledge_unknown_outcome: true}, true),
  );
}

export function startRun(id: string): Promise<{id: string; status: string}> {
  return fetchJson(`/api/runs/${id}/start`, jsonRequest({}, true));
}

// Cancel compensates failed setup so a newly created run cannot remain an
// orphan draft.
export function cancelRun(id: string): Promise<{id: string; status: string}> {
  return fetchJson(`/api/runs/${id}/cancel`, jsonRequest({}, true));
}

export interface StagedDocument {
  id: string;
  title: string;
  sha256: string;
  byte_size: number;
  mime_type: string;
  extraction_tool: string;
}

export function stageDocument(file: File): Promise<StagedDocument> {
  const body = new FormData();
  body.set('file', file);
  body.set('consent', 'true');
  return fetchJson('/api/documents', {
    method: 'POST',
    headers: clientHeaders(),
    body,
  });
}

type StatusInput = string | null | undefined;
type ActiveStatus = Extract<RunStatus, 'queued' | 'running' | 'synthesizing'>;
type FailureStatus = Extract<RunStatus, 'failed' | 'blocked'>;
export type TerminalNonCompletedStatus = FailureStatus | 'cancelled';

// Paused runs have started but are not progressing.
export function isActiveStatus(status: StatusInput): status is ActiveStatus {
  return (
    status === 'queued' || status === 'running' || status === 'synthesizing'
  );
}

export function isFailureStatus(status: StatusInput): status is FailureStatus {
  return status === 'failed' || status === 'blocked';
}

export function isDraftStatus(status: StatusInput): status is 'draft' {
  return status === 'draft';
}

export function isCompletedStatus(status: StatusInput): status is 'completed' {
  return status === 'completed';
}

export function isCancelledStatus(status: StatusInput): status is 'cancelled' {
  return status === 'cancelled';
}

// Terminal runs without a report need a distinct end-state view.
export function isTerminalNonCompletedStatus(
  status: StatusInput,
): status is TerminalNonCompletedStatus {
  return isFailureStatus(status) || isCancelledStatus(status);
}

export function isTerminalStatus(
  status: StatusInput,
): status is TerminalNonCompletedStatus | 'completed' {
  return isTerminalNonCompletedStatus(status) || isCompletedStatus(status);
}

export function isStoppableStatus(
  status: StatusInput,
): status is ActiveStatus | 'paused' {
  return isActiveStatus(status) || status === 'paused';
}

// Terminal failure/block does not mean start succeeded.
export function isStartedStatus(
  status: StatusInput,
): status is ActiveStatus | 'paused' | 'completed' {
  return isStoppableStatus(status) || isCompletedStatus(status);
}

// Retain failed/blocked start receipts so retries cannot create duplicate runs.
export function retiresStartIntent(
  status: StatusInput,
): status is ActiveStatus | 'paused' | 'completed' | 'cancelled' {
  return isStartedStatus(status) || isCancelledStatus(status);
}

export type RunActivity = 'active' | 'inactive' | 'unknown';

// Unknown status must not prematurely choose live progress or results.
export function runActivity(status: StatusInput): RunActivity {
  if (!status) return 'unknown';
  return isActiveStatus(status) ? 'active' : 'inactive';
}

function getRunList<T>(id: string, key: string): Promise<T[]> {
  return fetchField<string, T[]>(`/api/runs/${id}/${key}`, key, {
    headers: clientHeaders(),
  });
}

export function getHypotheses(id: string): Promise<Hypothesis[]> {
  return getRunList<Hypothesis>(id, 'hypotheses');
}

export function getEvidence(id: string): Promise<Evidence[]> {
  return getRunList<Evidence>(id, 'evidence');
}

export function getMatches(id: string): Promise<MatchRow[]> {
  return getRunList<MatchRow>(id, 'matches');
}

export function getReviews(id: string): Promise<Review[]> {
  return getRunList<Review>(id, 'reviews');
}

export function getSafety(id: string): Promise<SafetyDecision[]> {
  return getRunList<SafetyDecision>(id, 'safety');
}

export function getClaimEvidence(id: string): Promise<ClaimEvidenceRow[]> {
  return fetchField<'claim_evidence', ClaimEvidenceRow[]>(
    `/api/runs/${id}/claim-evidence`,
    'claim_evidence',
    {headers: clientHeaders()},
  );
}

export function uploadRunDocument(
  runId: string,
  file: File,
): Promise<{
  id: string;
  indexed: boolean;
  sha256: string;
  byte_size: number;
  mime_type: string;
  extraction_tool: string;
}> {
  const body = new FormData();
  body.set('file', file);
  body.set('consent', 'true');
  return fetchJson(`/api/runs/${runId}/attachments/upload`, {
    method: 'POST',
    headers: clientHeaders(),
    body,
  });
}

// The backend uses 404 for a report not yet generated.
export async function getReport(id: string): Promise<Report | null> {
  const res = await fetchWithSession(`${API_BASE_URL}/api/runs/${id}/report`, {
    headers: clientHeaders(),
  });
  if (res.status === 404) return null;
  return parseJson<Report>(res);
}

type InterviewFrame =
  | {type: 'reasoning'; content: string}
  | {type: 'chunk'; content: string}
  | {type: 'interview'; interview: Interview}
  | {type: 'error'; detail: string};

export interface InterviewSinks {
  onReasoning?: (fragment: string) => void;
  onProse?: (fragment: string) => void;
}

async function streamInterviewTurn(
  path: string,
  body: unknown,
  sinks: InterviewSinks = {},
  method = 'POST',
  signal?: AbortSignal,
): Promise<Interview> {
  let interview: Interview | undefined;
  for await (const frame of streamJson<InterviewFrame>(
    path,
    body,
    signal,
    method,
  )) {
    switch (frame.type) {
      case 'interview':
        interview = frame.interview;
        break;
      case 'error':
        throw new Error(frame.detail);
      case 'reasoning':
        sinks.onReasoning?.(frame.content);
        break;
      case 'chunk':
        sinks.onProse?.(frame.content);
    }
  }
  if (!interview) {
    throw new Error('The Agent could not continue the interview.');
  }
  return interview;
}

export function createInterview(
  researchChallenge: string,
  sinks?: InterviewSinks,
  documentIds: string[] = [],
  signal?: AbortSignal,
): Promise<Interview> {
  return streamInterviewTurn(
    '/api/interviews',
    {
      research_challenge: researchChallenge,
      document_ids: documentIds,
    },
    sinks,
    'POST',
    signal,
  );
}

export function addInterviewTurn(
  interviewId: string,
  content: string,
  sinks?: InterviewSinks,
  documentIds: string[] = [],
  signal?: AbortSignal,
): Promise<Interview> {
  return streamInterviewTurn(
    `/api/interviews/${interviewId}/turns`,
    {content, document_ids: documentIds},
    sinks,
    'POST',
    signal,
  );
}

export function editInterviewTurn(
  interviewId: string,
  turnId: number,
  content: string,
  sinks?: InterviewSinks,
  signal?: AbortSignal,
): Promise<Interview> {
  return streamInterviewTurn(
    `/api/interviews/${interviewId}/turns/${turnId}`,
    {content},
    sinks,
    'PUT',
    signal,
  );
}

export function retryInterviewTurn(
  interviewId: string,
  turnId: number,
  sinks?: InterviewSinks,
  signal?: AbortSignal,
): Promise<Interview> {
  return streamInterviewTurn(
    `/api/interviews/${interviewId}/turns/${turnId}/retry`,
    {},
    sinks,
    'POST',
    signal,
  );
}

export function listInterviews(): Promise<ChatSummary[]> {
  return fetchJson('/api/interviews', {headers: clientHeaders()});
}

export function openExampleChat(runId: string): Promise<Interview> {
  return fetchJson(`/api/runs/${runId}/example-chat`, {
    method: 'POST',
    headers: clientHeaders(),
  });
}

export function getInterview(interviewId: string): Promise<Interview> {
  return fetchJson(`/api/interviews/${interviewId}`, {
    headers: clientHeaders(),
  });
}

// The request's lab_constraints is optional (the server keeps the stored list),
// unlike the response's.
export type InterviewFieldsEdit = Omit<
  Interview['fields'],
  'title' | 'lab_constraints'
> &
  Partial<Pick<Interview['fields'], 'lab_constraints'>>;

export function editInterviewFields(
  interviewId: string,
  fields: InterviewFieldsEdit,
): Promise<Interview> {
  return fetchJson(`/api/interviews/${interviewId}/fields`, {
    ...jsonRequest(fields, true),
    method: 'PUT',
  });
}

export type {QaSource, RunMessage} from './wire_runs';

export interface QaSinks {
  // The manifest arrives before answer chunks, fixing their evidence context.
  onSources?: (sources: QaSource[]) => void;
  onReasoning?: (fragment: string) => void;
  onChunk?: (fragment: string) => void;
}

type AskFrame =
  | {type: 'sources'; sources: QaSource[]}
  | {type: 'reasoning'; content: string}
  | {type: 'chunk'; content: string}
  | {type: 'done'; question_id: number}
  | {type: 'error'; message: string};

// Aborted partial Q&A answers are never persisted.
export async function askRunQuestion(
  runId: string,
  question: string,
  sinks: QaSinks = {},
  signal?: AbortSignal,
  revisionId?: number,
): Promise<number | undefined> {
  let questionId: number | undefined;
  for await (const frame of streamJson<AskFrame>(
    revisionId === undefined
      ? `/api/runs/${runId}/messages/ask`
      : `/api/runs/${runId}/messages/${revisionId}/revise`,
    {question: question || undefined},
    signal,
  )) {
    switch (frame.type) {
      case 'sources':
        sinks.onSources?.(frame.sources);
        break;
      case 'reasoning':
        sinks.onReasoning?.(frame.content);
        break;
      case 'chunk':
        sinks.onChunk?.(frame.content);
        break;
      case 'done':
        questionId = frame.question_id;
        break;
      case 'error':
        throw new Error(frame.message);
    }
  }
  return questionId;
}

export function getRunMessages(runId: string): Promise<RunMessage[]> {
  return fetchField(`/api/runs/${runId}/messages`, 'messages', {
    headers: clientHeaders(),
  });
}

export type StartAnnouncementSinks = Pick<QaSinks, 'onReasoning' | 'onChunk'>;

export interface StartAnnouncement {
  fallback: boolean;
}

type StartFrame =
  | {type: 'reasoning'; content: string}
  | {type: 'chunk'; content: string}
  | {type: 'done'; prompt_id: number; fallback: boolean};

// Announcement failure cannot change an already-started run.
export async function announceRunStart(
  runId: string,
  prompt: string,
  sinks: StartAnnouncementSinks = {},
  signal?: AbortSignal,
): Promise<StartAnnouncement | null> {
  let outcome: StartAnnouncement | null = null;
  for await (const frame of streamJson<StartFrame>(
    `/api/runs/${runId}/messages/started`,
    {prompt},
    signal,
  )) {
    if (frame.type === 'done') {
      outcome = {fallback: frame.fallback};
    } else {
      const sink =
        frame.type === 'reasoning' ? sinks.onReasoning : sinks.onChunk;
      sink?.(frame.content);
    }
  }
  return outcome;
}

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL as string) || '';

export function clientHeaders(): Record<string, string> {
  return {'X-Client-ID': getClientId()};
}

// BYOK credentials and model choices travel only in request headers. The
// supervisor's provider and key are sent only when it is not the worker's.
export function byokHeaders(): Record<string, string> {
  for (const tier of ['worker', 'supervisor'] as const) {
    const choice = getStoredModel(tier);
    if (choice?.custom && !getStoredApiKey(choice.provider)) {
      throw new Error('Custom models require your own API key in Settings');
    }
  }
  const routes = resolveByokRoutes();
  if (!routes) return {};
  const {worker, supervisor} = routes;
  for (const tier of ['worker', 'supervisor'] as const) {
    if (getStoredModel(tier)?.custom && !routes[tier].model?.trim()) {
      throw new Error('Enter and check your custom model ID in Settings');
    }
  }
  return {
    'X-LLM-API-Key': worker.apiKey,
    'X-LLM-Provider': worker.provider,
    // An omitted model selects the provider default on the backend.
    ...(worker.model ? {'X-LLM-Model': worker.model} : {}),
    ...(supervisor.model ? {'X-LLM-Supervisor-Model': supervisor.model} : {}),
    ...(supervisor.provider !== worker.provider
      ? {
          'X-LLM-Supervisor-Provider': supervisor.provider,
          'X-LLM-Supervisor-API-Key': supervisor.apiKey,
        }
      : {}),
  };
}

export class HttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'HttpError';
  }
}

async function responseErrorMessage(res: Response): Promise<string> {
  const text = await res.text().catch(() => res.statusText);
  if (res.status === 500 && !text.trim()) return 'API unavailable';
  // The API writes string details for the reader; validation lists are not.
  let detail: unknown;
  try {
    detail = (JSON.parse(text) as {detail?: unknown} | null)?.detail;
  } catch {
    // Non-JSON error bodies retain their ordinary status/message fallback.
  }
  if (typeof detail === 'string' && detail) return detail;
  if (detail !== undefined) {
    return `Request failed (${res.status}${res.statusText ? ` ${res.statusText}` : ''})`;
  }
  return `${res.status} ${text || res.statusText}`;
}

export async function fetchWithSession(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (error) {
    logFetchFailure(url, init, 'network');
    throw error;
  }
  if (!res.ok) logFetchFailure(url, init, res.status);
  return res;
}

function logFetchFailure(
  url: string,
  init: RequestInit | undefined,
  status: number | 'network',
) {
  if (typeof window === 'undefined') return;
  let path = 'unknown';
  try {
    path = new URL(url, window.location.origin).pathname;
  } catch {
    // Preserve the original transport error for malformed endpoints.
  }
  // A failed diagnostic write must not recursively generate another one.
  if (path.startsWith('/api/logs')) return;
  emitDiagnostic({
    stage: 'fetch_failed',
    level: 'warning',
    payload: {method: init?.method ?? 'GET', path, status},
  });
}

async function parseJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    throw new HttpError(await responseErrorMessage(res), res.status);
  }
  return (await res.json()) as T;
}

export async function fetchJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetchWithSession(`${API_BASE_URL}${path}`, init);
  return parseJson<T>(res);
}

export async function fetchField<K extends string, T>(
  path: string,
  field: K,
  init?: RequestInit,
): Promise<T> {
  const data = await fetchJson<Record<K, T>>(path, init);
  return data[field];
}

// Public endpoints omit identity; scoped endpoints explicitly opt in.
export function jsonRequest(
  body: unknown,
  includeClientId = false,
): RequestInit & {headers: Record<string, string>} {
  return {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(includeClientId ? clientHeaders() : {}),
    },
    body: JSON.stringify(body),
  };
}

export async function* readSseFrames<T>(res: Response): AsyncGenerator<T> {
  if (!res.ok || !res.body) {
    throw new HttpError(await responseErrorMessage(res), res.status);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let pending = '';
  for (;;) {
    const {done, value} = await reader.read();
    pending += decoder.decode(value, {stream: !done});
    const frames = pending.split('\n\n');
    pending = frames.pop() || '';
    for (const frame of frames) {
      const data = frame
        .split('\n')
        .find(line => line.startsWith('data: '))
        ?.slice(6);
      if (data) yield JSON.parse(data) as T;
    }
    if (done) break;
  }
}

async function* streamJson<T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
  method = 'POST',
): AsyncGenerator<T> {
  const init = jsonRequest(body, true);
  const res = await fetchWithSession(`${API_BASE_URL}${path}`, {
    ...init,
    method,
    signal,
    headers: {...init.headers, ...byokHeaders()},
  });
  yield* readSseFrames<T>(res);
}
