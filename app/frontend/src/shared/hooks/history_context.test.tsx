import {act, render, screen, waitFor} from '@testing-library/react';
import {MemoryRouter, useNavigate} from 'react-router-dom';
import {StrictMode, type ReactNode} from 'react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type MockedFunction,
} from 'vitest';
import {
  listInterviews,
  loadRunHistory,
  type ChatSummary,
} from '@/shared/api/runs';
import {deferred} from '@/shared/testing/deferred';
import {makeChat, makeRun} from '@/shared/testing/fixtures';
import {CHATS_CHANGED_EVENT, RUNS_CHANGED_EVENT} from '@/shared/lib/dom_events';
import {
  ChatHistoryProvider,
  RunHistoryProvider,
  useChatHistoryContext,
  useRunHistoryContext,
} from './history_context';

vi.mock('@/shared/api/runs', async importActual => {
  const actual = await importActual<typeof import('@/shared/api/runs')>();
  return {...actual, listInterviews: vi.fn(), loadRunHistory: vi.fn()};
});

const chat = (id: string): ChatSummary =>
  makeChat({id, title: id, challenge: id, updated_at: 1}, 'active');

function RunProbe() {
  const {history} = useRunHistoryContext();
  return <span data-testid="rows">{history.map(row => row.id).join(',')}</span>;
}

function ChatProbe() {
  const {chats} = useChatHistoryContext();
  return <span data-testid="rows">{chats.map(row => row.id).join(',')}</span>;
}

function Navigate() {
  const navigate = useNavigate();
  return <button onClick={() => navigate('/other')}>Navigate</button>;
}

function renderHistory(
  provider: (props: {children: ReactNode}) => ReactNode,
  probe: ReactNode,
  strict = false,
) {
  const Provider = provider;
  const content = (
    <MemoryRouter>
      <Provider>
        {probe}
        <Navigate />
      </Provider>
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{content}</StrictMode> : content);
}

interface HistoryFixture<T> {
  name: string;
  provider: (props: {children: ReactNode}) => ReactNode;
  probe: ReactNode;
  event: string;
  load: MockedFunction<() => Promise<T[]>>;
  row: (id: string) => T;
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(loadRunHistory).mockResolvedValue([]);
  vi.mocked(listInterviews).mockResolvedValue([]);
});

function historyContract<T>(history: HistoryFixture<T>) {
  it(`keeps newer ${history.name} history when an earlier load finishes later`, async () => {
    const older = deferred<T[]>();
    history.load.mockReturnValueOnce(older.promise);
    renderHistory(history.provider, history.probe);
    history.load.mockResolvedValue([history.row('newer')]);
    act(() => {
      window.dispatchEvent(new Event(history.event));
    });
    await waitFor(() =>
      expect(screen.getByTestId('rows')).toHaveTextContent('newer'),
    );
    await act(async () => older.resolve([history.row('older')]));
    expect(screen.getByTestId('rows')).toHaveTextContent('newer');
  });
}

historyContract({
  name: 'run',
  provider: RunHistoryProvider,
  probe: <RunProbe />,
  event: RUNS_CHANGED_EVENT,
  load: vi.mocked(loadRunHistory),
  row: (id: string) => makeRun({id}),
});

historyContract({
  name: 'chat',
  provider: ChatHistoryProvider,
  probe: <ChatProbe />,
  event: CHATS_CHANGED_EVENT,
  load: vi.mocked(listInterviews),
  row: chat,
});

describe('active run polling', () => {
  const loadMock = vi.mocked(loadRunHistory);

  function HistoryProbe() {
    const {history} = useRunHistoryContext();
    return <span data-testid="count">{history.length}</span>;
  }

  function renderProvider() {
    return render(
      <MemoryRouter>
        <RunHistoryProvider>
          <HistoryProbe />
        </RunHistoryProvider>
      </MemoryRouter>,
    );
  }

  beforeEach(() => {
    vi.useFakeTimers({shouldAdvanceTime: true});
    loadMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('refreshes on a timer while a run is still executing', async () => {
    // Run phases advance through polling without navigation or start events.
    loadMock.mockResolvedValue([makeRun({status: 'running'})]);
    renderProvider();
    await waitFor(() =>
      expect(screen.getByTestId('count')).toHaveTextContent('1'),
    );
    expect(loadMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(10_000);
    expect(loadMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(loadMock).toHaveBeenCalledTimes(3);
  });

  it('stops refreshing once the run reaches a terminal status', async () => {
    loadMock.mockResolvedValue([makeRun({status: 'running'})]);
    renderProvider();
    await waitFor(() =>
      expect(screen.getByTestId('count')).toHaveTextContent('1'),
    );
    expect(loadMock).toHaveBeenCalledTimes(1);

    loadMock.mockResolvedValue([makeRun({status: 'completed'})]);
    // A terminal refresh can already be in flight; verify eventual polling
    // quiescence.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    const callsOnceSettled = loadMock.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(loadMock).toHaveBeenCalledTimes(callsOnceSettled);
  });
});
