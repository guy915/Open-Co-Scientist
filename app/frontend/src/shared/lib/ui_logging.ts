// Persist browser failures so diagnostics survive reloads.
import {postAppLogs, type ClientLogRecord} from '@/shared/api/logs';
import {DIAGNOSTIC_EVENT, type DiagnosticDetail} from './diagnostic_events';
import {httpStatus} from './errors';
import {STORAGE_KEYS} from './safe_storage';

// Match the ingestion endpoint's one-minute window. Keep the deadline across
// same-tab reloads so navigation cannot restart a rejected submission burst.
const RATE_LIMIT_PAUSE_MS = 60_000;
const RATE_LIMIT_PAUSE_KEY = STORAGE_KEYS.logsPauseUntil;
let logPauseUntil = 0;

function pausedUntil(): number {
  try {
    const stored = Number(window.sessionStorage.getItem(RATE_LIMIT_PAUSE_KEY));
    return Math.max(logPauseUntil, Number.isFinite(stored) ? stored : 0);
  } catch {
    return logPauseUntil;
  }
}

function pauseLogging(): void {
  logPauseUntil = Date.now() + RATE_LIMIT_PAUSE_MS;
  try {
    window.sessionStorage.setItem(RATE_LIMIT_PAUSE_KEY, String(logPauseUntil));
  } catch {
    // A denied storage write must never interrupt the page being diagnosed.
  }
}

// Logging is best-effort: reporting an unavailable API must never break the
// page being diagnosed.
function postBestEffort(records: ClientLogRecord[]): void {
  if (Date.now() < pausedUntil()) return;
  void postAppLogs(records).catch(error => {
    if (httpStatus(error) === 429) pauseLogging();
    // Reporting API failure must not itself break the page.
  });
}

export function logUiError(message: string, detail?: string): void {
  const full = detail ? `${message} | ${detail}` : message;
  postBestEffort([{message: full, level: 'error', logger: 'error'}]);
}

export function logNavigation(message: string): void {
  postBestEffort([{message, logger: 'navigation'}]);
}

function diagnosticRecord(detail: DiagnosticDetail): ClientLogRecord {
  const payload = detail.payload || {};
  const suffix = Object.keys(payload).length
    ? ` ${JSON.stringify(payload)}`
    : '';
  return {
    message: `${detail.stage}${suffix}`,
    level:
      detail.level === 'error' || detail.level === 'warning'
        ? detail.level
        : 'info',
    logger: 'session',
    ...(detail.runId ? {run_id: detail.runId} : {}),
  };
}

// Installed before the app mounts, so no record depends on which shell
// controls happen to be on screen.
export function installDiagnosticLogging(): () => void {
  function onDiagnostic(event: Event) {
    const {detail} = event as CustomEvent<DiagnosticDetail | undefined>;
    if (!detail?.stage) return;
    postBestEffort([diagnosticRecord(detail)]);
  }
  window.addEventListener(DIAGNOSTIC_EVENT, onDiagnostic);
  return () => window.removeEventListener(DIAGNOSTIC_EVENT, onDiagnostic);
}

export function logModalOpen(name: string): void {
  postBestEffort([
    {message: `modal_open: ${name.slice(0, 80)}`, logger: 'modal'},
  ]);
}

const INTERACTIVE_SELECTOR =
  'button, a, select, summary, [role="button"], ' +
  'input[type="button"], input[type="submit"]';

// Labels orient diagnostics, never capture a transcript; keep them well below
// the ingestion limit.
const LABEL_MAX_CHARS = 80;

function interactionLabel(control: Element): string {
  const raw =
    control.getAttribute('aria-label') ||
    control.textContent ||
    control.getAttribute('title') ||
    '';
  const label = raw.replace(/\s+/g, ' ').trim();
  return label.length > LABEL_MAX_CHARS
    ? `${label.slice(0, LABEL_MAX_CHARS)}…`
    : label;
}

// Batch interaction chatter to reduce writes; errors/navigation remain
// immediate.
const INTERACTION_FLUSH_MS = 2_000;

// Keep batches below the ingestion endpoint 50-record limit.
const INTERACTION_FLUSH_COUNT = 20;

let pendingInteractions: ClientLogRecord[] = [];
let interactionFlushTimer: number | null = null;

function flushInteractions(): void {
  if (interactionFlushTimer !== null) {
    window.clearTimeout(interactionFlushTimer);
    interactionFlushTimer = null;
  }
  if (!pendingInteractions.length) return;
  const batch = pendingInteractions;
  pendingInteractions = [];
  postBestEffort(batch);
}

function logUiInteraction(message: string): void {
  pendingInteractions.push({message, logger: 'interaction'});
  if (pendingInteractions.length >= INTERACTION_FLUSH_COUNT) {
    flushInteractions();
    return;
  }
  if (interactionFlushTimer === null) {
    interactionFlushTimer = window.setTimeout(
      flushInteractions,
      INTERACTION_FLUSH_MS,
    );
  }
}

// Capture listeners observe controls before handlers stop propagation or React
// removes the target.
export function installUiInteractionLogging(): () => void {
  function onClick(event: Event) {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const control = target.closest(INTERACTIVE_SELECTOR);
    if (!control) return;
    const tag = control.tagName.toLowerCase();
    logUiInteraction(`click: "${interactionLabel(control)}" (${tag})`);
  }

  function onSubmit(event: Event) {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const label =
      form.getAttribute('aria-label') || form.getAttribute('name') || form.id;
    logUiInteraction(`submit: "${label || 'form'}" (form)`);
  }

  document.addEventListener('click', onClick, true);
  document.addEventListener('submit', onSubmit, true);
  window.addEventListener('pagehide', flushInteractions);
  return () => {
    document.removeEventListener('click', onClick, true);
    document.removeEventListener('submit', onSubmit, true);
    window.removeEventListener('pagehide', flushInteractions);
    flushInteractions();
  };
}

// ResizeObserver loop notices lack exception stacks and do not imply failed
// layout; suppress only these notices, never real callback exceptions.
const BENIGN_ERROR_PATTERN = /^(Uncaught )?ResizeObserver loop /;

export function installUiErrorLogging(): () => void {
  function onError(event: ErrorEvent) {
    if (BENIGN_ERROR_PATTERN.test(event.message)) return;
    const where = event.filename
      ? ` (${event.filename}:${event.lineno ?? 0})`
      : '';
    logUiError(`uncaught error: ${event.message}${where}`);
  }

  function onRejection(event: Event) {
    const {reason} = event as Event & {reason?: unknown};
    logUiError(`unhandled rejection: ${String(reason)}`);
  }

  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
  return () => {
    window.removeEventListener('error', onError);
    window.removeEventListener('unhandledrejection', onRejection);
  };
}
