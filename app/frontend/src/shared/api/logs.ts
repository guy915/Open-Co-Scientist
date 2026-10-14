// Identity headers are required: unidentified deployed callers see no owned
// logs, and their submitted records become ownerless.
import {clientHeaders, fetchJson, jsonRequest} from './runs';
import type {ClientLogRecord} from './wire_system';

export interface AppLogRecord {
  id: number;
  // Timestamps are Unix seconds, unlike browser millisecond clocks.
  created_at: number;
  level: string;
  levelno: number;
  logger: string;
  message: string;
  run_id: string | null;
  exc_text: string | null;
}

export interface AppLogsPayload {
  // The window contains the newest matches in oldest-first order.
  logs: AppLogRecord[];
  // Table high-water mark; session baselines anchor to it.
  last_id: number;
  total: number;
  // Use the server cursor-window count; subtracting whole-table totals breaks
  // when retention or scoped clears remove earlier rows.
  session_total: number;
}

export function getAppLogs(
  afterId = 0,
  limit = 1000,
  minLevel?: string,
): Promise<AppLogsPayload> {
  return fetchJson(
    `/api/logs?after_id=${afterId}&limit=${limit}` +
      (minLevel ? `&min_level=${minLevel}` : ''),
    {headers: clientHeaders()},
  );
}

export type {ClientLogRecord} from './wire_system';

export async function postAppLogs(
  records: ClientLogRecord[],
): Promise<{added: number; last_id: number}> {
  return fetchJson<{added: number; last_id: number}>(
    '/api/logs',
    jsonRequest({records}, /*includeClientId=*/ true),
  );
}
