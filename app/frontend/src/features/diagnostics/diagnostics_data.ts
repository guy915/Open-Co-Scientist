import {type AppLogRecord} from '@/shared/api/logs';
import {type DiagnosticLevel} from '@/shared/lib/diagnostic_events';
import {formatClockTime} from '@/shared/lib/time';

type DiagnosticLogLevel = DiagnosticLevel;

export interface DiagnosticLogEntry {
  id: number;
  // Session-relative position; store IDs have gaps.
  number: number;
  time: string;
  run: string;
  stage: string;
  level: DiagnosticLogLevel;
  levelName: string;
  message: string;
  excText: string | null;
  payload: Record<string, unknown>;
}

// Server-owned records must not inflate the distinct research-run count.
const SERVER_LOG_SOURCE = 'Server';

export const EXPORT_LIMIT = 100;

export interface PersistedAppLogs {
  entries: DiagnosticLogEntry[];
  total: number;
}

function appLogLevel(record: AppLogRecord): DiagnosticLogLevel {
  if (record.levelno >= 40) return 'error';
  return record.levelno >= 30 ? 'warning' : 'info';
}

export function buildAppLogEntry(
  record: AppLogRecord,
  number: number,
): DiagnosticLogEntry {
  return {
    id: record.id,
    number,
    time: formatClockTime(record.created_at),
    run: record.run_id ? `Run ${record.run_id.slice(0, 8)}` : SERVER_LOG_SOURCE,
    stage: record.logger,
    level: appLogLevel(record),
    levelName: record.level,
    message: record.message,
    excText: record.exc_text,
    payload: {
      level: record.level,
      message: record.message,
      ...(record.exc_text ? {exc_text: record.exc_text} : {}),
    },
  };
}

export interface DiagnosticCounts {
  errorCount: number;
  warningCount: number;
  infoCount: number;
  runCount: number;
}

export function summarizeDiagnosticEntries(
  entries: DiagnosticLogEntry[],
): DiagnosticCounts {
  return {
    errorCount: entries.filter(entry => entry.level === 'error').length,
    warningCount: entries.filter(entry => entry.level === 'warning').length,
    infoCount: entries.filter(entry => entry.level === 'info').length,
    runCount: new Set(
      entries.map(({run}) => run).filter(run => run !== SERVER_LOG_SOURCE),
    ).size,
  };
}

// A pasted array lacks provenance/filter/ID context; keep the export preamble
// alongside its sliceable JSON marker.

const EXPORT_LOGS_MARKER = '## Logs (JSON)';

export interface DiagnosticExportContext {
  currentUrl?: string;
  userAgent?: string;
  exportedAt?: Date;
}

export interface DiagnosticExport {
  entries: DiagnosticLogEntry[];
  total: number;
  counts: DiagnosticCounts;
  context?: DiagnosticExportContext;
}

const UNAVAILABLE = 'Unavailable';

// Prose stays one line per paragraph: the export is pasted into issues, chats
// and models that reflow it, where hard wraps leave ragged half-lines.
function aboutSection(): string[] {
  return [
    '# Co-Scientist diagnostic export',
    '',
    '## About these logs',
    '',
    'Co-Scientist workbench diagnostic export, attached to a feedback submission. It is built from one durable, app-wide log: backend records from the API, the durable task workers and the `co_scientist` engine, one compact stage record per run event, and frontend records this browser posted. It is scoped to this browsing session (records that predate it are excluded; a tab reload keeps the session, closing the tab ends it) and to what this caller may see. Share the whole export when reporting a problem — the preamble is the context a reader would otherwise have to guess at.',
    '',
  ];
}

function tracksSection(): string[] {
  return [
    '## What this tracks',
    '',
    '- **Run stages:** `lifecycle`, `safety.intake`, `supervisor.plan`, `literature_review`, `generate`, `reflection`, `proximity`, `ranking`, `evolve`, `meta_review`, `deep_verification`, `citation_audit`, `research_overview`, `report`, `status`',
    '- **Backend:** app, worker and store records, plus engine records at INFO and above',
    '- **Frontend (`ui.*`):** page loads and route changes, uncaught errors, unhandled rejections, React render errors, and control interactions',
    '- **Deliberately absent:**',
    '  - per-call HTTP/LLM chatter below WARNING, which is never persisted',
    '  - verbatim repeats of a record within a 10-minute window',
    '',
  ];
}

function resolveContext(context: DiagnosticExportContext) {
  return {
    exportedAt: (context.exportedAt ?? new Date()).toLocaleString(),
    currentUrl: context.currentUrl ?? UNAVAILABLE,
    userAgent: context.userAgent ?? UNAVAILABLE,
  };
}

function sessionSection(
  {total}: DiagnosticExport,
  exported: DiagnosticLogEntry[],
  context: DiagnosticExportContext,
): string[] {
  const oldest = exported[0];
  const first = oldest ? `#${oldest.number} at ${oldest.time}` : 'none';
  const {exportedAt, currentUrl, userAgent} = resolveContext(context);
  return [
    '## Session details',
    '',
    `- **Exported:** ${exportedAt}`,
    `- **Current URL:** ${currentUrl}`,
    `- **Browser:** ${userAgent}`,
    `- **Records this session:** ${total}`,
    `- **In this export:** ${exported.length} (newest ${EXPORT_LIMIT})`,
    `- **Oldest exported record:** ${first}`,
    '',
  ];
}

function statisticsSection(counts: DiagnosticCounts): string[] {
  return [
    '## Statistics (loaded window)',
    '',
    '| Errors | Warnings | Info | Distinct runs |',
    '| --- | --- | --- | --- |',
    `| ${counts.errorCount} | ${counts.warningCount} | ${counts.infoCount} | ${counts.runCount} |`,
    '',
  ];
}

function legendSection(): string[] {
  return [
    '## Field legend',
    '',
    '| Field | Meaning |',
    '| --- | --- |',
    '| `id` | Persisted store row id used by API `after_id` cursors; gapped wherever ids were pruned or never persisted. |',
    '| `number` | Position in the filtered stream, numbered within this session. |',
    '| `level` | `error` is ERROR/CRITICAL (40+), `warning` is WARNING (30), `info` is everything below; `levelName` carries the exact name. |',
    '| `run` | "Run <first 8 chars>", or "Server" when no run owns it; Distinct runs counts only run-owned records. |',
    '| `stage` | The emitting logger (`app.run_stage`, `ui.error`, `uvicorn`, ...). |',
    '| `excText` | Formatted traceback, when the record carried one. |',
    '',
  ];
}

export function formatDiagnosticExport(input: DiagnosticExport): string {
  const context = input.context ?? {};
  const exported = input.entries.slice(-EXPORT_LIMIT);
  return [
    ...aboutSection(),
    ...tracksSection(),
    ...sessionSection(input, exported, context),
    ...statisticsSection(input.counts),
    ...legendSection(),
    EXPORT_LOGS_MARKER,
    '',
    '```json',
    JSON.stringify(exported, null, 2),
    '```',
  ].join('\n');
}

export function browserExportContext(): DiagnosticExportContext {
  if (typeof window === 'undefined') return {};
  return {
    currentUrl: window.location.href,
    userAgent: window.navigator.userAgent,
  };
}
