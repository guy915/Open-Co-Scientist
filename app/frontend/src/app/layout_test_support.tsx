import {vi} from 'vitest';
import type {Run} from '@/shared/api/runs';
import {makeChat, makeRunWithSummary} from '@/shared/testing/fixtures';
import {renderWithProviders} from '@/shared/testing/render';
import {resetSessionBaselineForTest} from '@/features/diagnostics/diagnostics_test_support';
import {Layout} from './layout';

// Hoist mocks before imports bind real network modules.
const apiMock = vi.hoisted(() => {
  const listDemoRuns = vi.fn();
  const listRuns = vi.fn();
  const listInterviews = vi.fn();
  const getRunEvents = vi.fn();
  // Keep real merge policy while replacing only network methods.
  const loadRunHistory = vi.fn(async () => {
    const [owned, demo] = await Promise.all([
      listRuns().catch(() => []),
      listDemoRuns().catch(() => []),
    ]);
    const byId = new Map(
      ([...owned, ...demo] as Run[]).map(run => [run.id, run]),
    );
    return [...byId.values()].sort((a, b) => b.updated_at - a.updated_at);
  });
  return {
    listDemoRuns,
    listRuns,
    listInterviews,
    getRunEvents,
    loadRunHistory,
  };
});

const systemApiMock = vi.hoisted(() => ({getSystemStatus: vi.fn()}));

const logsApiMock = vi.hoisted(() => ({
  getAppLogs: vi.fn(),
  postAppLogs: vi.fn(),
}));

// Keep lifecycle helpers real so network mocks cannot change status semantics.
vi.mock('@/shared/api/runs', async importOriginal => ({
  ...(await importOriginal<typeof import('@/shared/api/runs')>()),
  ...apiMock,
}));

vi.mock('@/shared/api/system', async importOriginal => ({
  ...(await importOriginal<typeof import('@/shared/api/system')>()),
  ...systemApiMock,
}));

// Keep real log constants while replacing network calls.
vi.mock('@/shared/api/logs', async importOriginal => ({
  ...(await importOriginal<typeof import('@/shared/api/logs')>()),
  ...logsApiMock,
}));

export {apiMock, logsApiMock};

export function renderLayout(path = '/') {
  return renderWithProviders(
    <Layout>
      <main>Workspace content</main>
    </Layout>,
    {path, theme: true},
  );
}

export function installLayoutMocks() {
  // Reset the session anchor so isolated suites cannot inherit log history.
  resetSessionBaselineForTest();
  window.localStorage.clear();
  window.localStorage.setItem('cosci-theme', 'dark');
  document.documentElement.dataset.theme = '';
  document.documentElement.classList.remove('dark');
  apiMock.listRuns.mockResolvedValue([]);
  apiMock.listInterviews.mockResolvedValue([
    makeChat(
      {
        id: 'chat-ferroptosis',
        challenge:
          'Generate testable hypotheses for ferroptosis in pancreatic cancer ' +
          'cells.',
      },
      'active',
    ),
  ]);
  apiMock.listDemoRuns.mockResolvedValue([
    makeRunWithSummary(
      {
        id: 'demo-ferroptosis',
        research_goal:
          'Generate testable hypotheses for ferroptosis in pancreatic cancer ' +
          'cells.',
      },
      'listed',
    ),
  ]);
  apiMock.getRunEvents.mockReset();
  apiMock.getRunEvents.mockResolvedValue([]);
  logsApiMock.getAppLogs.mockReset();
  logsApiMock.getAppLogs.mockResolvedValue({
    logs: [],
    last_id: 0,
    total: 0,
    session_total: 0,
  });
  // Initial empty logs establish cursor zero; subsequent fixtures then belong
  // to this session.
  logsApiMock.getAppLogs.mockResolvedValueOnce({
    logs: [],
    last_id: 0,
    total: 0,
    session_total: 0,
  });
  logsApiMock.postAppLogs.mockReset();
  logsApiMock.postAppLogs.mockResolvedValue({added: 1, last_id: 1});
  systemApiMock.getSystemStatus.mockReset();
  systemApiMock.getSystemStatus.mockResolvedValue({
    llm_backend: 'real',
    provider: 'engine',
    model_name: 'test/model',
  });
}
