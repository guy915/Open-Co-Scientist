import {useEffect, useRef} from 'react';
import {getAppLogs, type AppLogsPayload} from '@/shared/api/logs';
import {useLocation} from 'react-router-dom';
import {logNavigation} from '@/shared/lib/ui_logging';
import {
  EXPORT_LIMIT,
  browserExportContext,
  buildAppLogEntry,
  formatDiagnosticExport,
  summarizeDiagnosticEntries,
  type DiagnosticLogEntry,
  type PersistedAppLogs,
} from './diagnostics_data';
import {
  readStorage,
  STORAGE_KEYS,
  writeStorage,
} from '@/shared/lib/safe_storage';
import {nowSeconds} from '@/shared/lib/time';
import {sessionBaselineMemory} from './diagnostics_baseline_state';

// The feedback export is scoped to records after this anchor, so it must be
// taken when the shell mounts, not when feedback is first submitted.
function useSessionLogAnchor() {
  useEffect(() => {
    getAppLogs(sessionAfterId(), 1, 'WARNING')
      .then(ensureSessionBaseline)
      .catch(() => {
        // Without an anchor the export falls back to its first fetch.
      });
  }, []);
}

// Mounted for the shell lifetime; renders nothing.
export function SessionDiagnostics() {
  useSessionLogAnchor();
  useNavigationLog();
  return null;
}

// sessionStorage preserves the log anchor across tab reloads but ends it on tab
// close; each tab owns its view.
const BASELINE_KEY = STORAGE_KEYS.logsBaseline;

interface SessionBaseline {
  id: number;
}

function readBaseline(): SessionBaseline | null {
  const raw = readStorage('session', BASELINE_KEY);
  if (raw) {
    try {
      return JSON.parse(raw) as SessionBaseline;
    } catch {
      // A corrupt anchor falls back to the memory one.
    }
  }
  return sessionBaselineMemory.current;
}

function writeBaseline(baseline: SessionBaseline): void {
  sessionBaselineMemory.current = baseline;
  writeStorage('session', BASELINE_KEY, JSON.stringify(baseline));
}

function sessionAfterId(): number {
  return readBaseline()?.id ?? 0;
}

// Feedback attaches this export of the current browsing session.
export async function sessionDiagnosticExport(): Promise<string> {
  let entries: DiagnosticLogEntry[] = [];
  let total = 0;
  try {
    const afterId = sessionAfterId();
    const payload = await getAppLogs(afterId, EXPORT_LIMIT);
    ensureSessionBaseline(payload);
    const loaded = buildLoadedLogs(payload, afterId);
    entries = loaded.entries;
    total = loaded.total;
  } catch {
    entries = [
      buildAppLogEntry(
        {
          id: 0,
          created_at: nowSeconds(),
          level: 'WARNING',
          levelno: 30,
          logger: 'ui.feedback',
          message:
            'Session diagnostics were unavailable when feedback was submitted.',
          run_id: null,
          exc_text: null,
        },
        1,
      ),
    ];
    total = 1;
  }
  const format = () =>
    formatDiagnosticExport({
      entries,
      total,
      counts: summarizeDiagnosticEntries(entries),
      context: browserExportContext(),
    });
  let report = format();
  while (report.length > 100_000 && entries.length) {
    entries.shift();
    report = format();
  }
  return report;
}

// Re-anchor if ids restart below the cursor. Capture before disposal guards so
// a discarded mount cannot make later loads hide session records.
function ensureSessionBaseline(payload: AppLogsPayload): void {
  const baseline = readBaseline();
  if (baseline === null || payload.last_id < baseline.id) {
    writeBaseline({id: payload.last_id});
  }
}

// Use server session_total: subtracting table totals breaks after retention
// prunes. Number the newest row by that uncapped total.
function buildLoadedLogs(
  payload: AppLogsPayload,
  requestAfterId: number,
): PersistedAppLogs {
  const baseline = readBaseline() ?? {id: payload.last_id};
  const session = payload.logs.filter(record => record.id > baseline.id);
  // Responses issued before the current anchor counted pre-session rows and
  // must contribute nothing.
  const total = requestAfterId < baseline.id ? 0 : payload.session_total;
  const shown = session.slice(-EXPORT_LIMIT);
  const first = total - shown.length + 1;
  return {
    entries: shown.map((record, index) =>
      buildAppLogEntry(record, first + index),
    ),
    total,
  };
}

function useNavigationLog() {
  const {pathname} = useLocation();
  const lastLogged = useRef<string | null>(null);

  useEffect(() => {
    if (lastLogged.current === pathname) return;
    const message =
      lastLogged.current === null
        ? `page loaded at ${pathname}`
        : `navigated to ${pathname}`;
    lastLogged.current = pathname;
    logNavigation(message);
  }, [pathname]);
}
