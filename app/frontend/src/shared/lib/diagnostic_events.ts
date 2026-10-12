export const DIAGNOSTIC_EVENT = 'cosci-diagnostic-event';

export type DiagnosticLevel = 'info' | 'warning' | 'error';

export interface DiagnosticDetail {
  stage: string;
  // Persisted and served through the API: the real run ID, never a
  // goal-derived title that would disclose research content.
  runId?: string;
  level?: DiagnosticLevel;
  payload?: Record<string, unknown>;
}

// The one way to report a session diagnostic. An event, not a direct POST, so
// the api layer can report its own failures without importing the logger.
export function emitDiagnostic(detail: DiagnosticDetail): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(DIAGNOSTIC_EVENT, {detail}));
}
