import {exportedRecords} from '@/shared/testing/fixtures';
import {beforeEach, expect, it, vi} from 'vitest';
import {resetSessionBaselineForTest} from './diagnostics_test_support';
import {sessionDiagnosticExport} from './diagnostics';

const logsApiMock = vi.hoisted(() => ({getAppLogs: vi.fn()}));
vi.mock('@/shared/api/logs', async importOriginal => ({
  ...(await importOriginal<typeof import('@/shared/api/logs')>()),
  ...logsApiMock,
}));

beforeEach(() => {
  logsApiMock.getAppLogs.mockReset();
  resetSessionBaselineForTest();
});

it('preserves a useful export when fetching diagnostics fails', async () => {
  logsApiMock.getAppLogs.mockRejectedValue(new Error('API unavailable'));
  const text = await sessionDiagnosticExport();
  expect(text).toContain('Session diagnostics were unavailable');
  expect(text).toContain('## Session details');
  expect(exportedRecords<{id: number}>(text)).toHaveLength(1);
});

it('reset clears the memory fallback as well as session storage', async () => {
  logsApiMock.getAppLogs.mockResolvedValueOnce({
    logs: [],
    last_id: 42,
    total: 0,
    session_total: 0,
  });
  await sessionDiagnosticExport();

  resetSessionBaselineForTest();
  logsApiMock.getAppLogs.mockRejectedValueOnce(new Error('API unavailable'));
  await sessionDiagnosticExport();

  expect(logsApiMock.getAppLogs.mock.calls.at(-1)?.[0]).toBe(0);
});
