import {deferred} from '@/shared/testing/deferred';
import {makeChat, makeRunWithSummary} from '@/shared/testing/fixtures';
import {ProviderStack} from '@/shared/testing/render';
import {
  act,
  fireEvent,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {
  apiMock,
  installChatWorkspaceMocks,
  renderWorkspace,
} from './chat_workspace_test_helpers';
import {useChatSession} from './use_chat_session';
import {useChatRehydration} from './use_chat_rehydrate';

const pendingIntentMock = vi.hoisted(() => vi.fn());

vi.mock('./chat_session_start_run', async importOriginal => ({
  ...(await importOriginal<typeof import('./chat_session_start_run')>()),
  readPendingCreateIntent: pendingIntentMock,
}));

beforeEach(() => {
  installChatWorkspaceMocks();
  vi.clearAllMocks();
  pendingIntentMock.mockReset();
  pendingIntentMock.mockResolvedValue(undefined);
});

function completedInterview() {
  return {
    id: 'interview-1',
    client_id: 'client-1',
    status: 'completed' as const,
    fields: {
      research_challenge: 'Investigate glucose homeostasis.',
      focus_area: ['Cold-stress glucose regulation'],
      preferences: ['Prioritize mechanistic novelty'],
      title: 'Cold-stress glucose homeostasis',
    },
    current_question: null,
    turns: [
      {
        id: 1,
        role: 'user' as const,
        content: 'Investigate glucose homeostasis.',
        reasoning: null,
        fallback: false,
        created_at: 1,
      },
      {
        id: 2,
        role: 'agent' as const,
        content: 'I have enough detail to configure this research run.',
        reasoning: null,
        fallback: false,
        created_at: 2,
      },
    ],
    created_at: 1,
    updated_at: 2,
    completed_at: 2,
  };
}

function rehydrateSession(chatId: string) {
  return renderHook(
    ({id}) => {
      const session = useChatSession({
        reloadHistory: async () => undefined,
        onChatStarted: () => undefined,
        focusComposer: () => undefined,
        setToast: () => undefined,
        pubmedEnabled: true,
        webSearchEnabled: true,
      });
      useChatRehydration(session, id);
      return session;
    },
    {
      initialProps: {id: chatId},
      reactStrictMode: true,
      wrapper: ProviderStack,
    },
  );
}

it('ignores an old owned fetch after another chat has committed', async () => {
  const old = deferred<ReturnType<typeof completedInterview>>();
  const next = {
    ...completedInterview(),
    id: 'interview-2',
    turns: [
      {...completedInterview().turns[0], content: 'Current private transcript'},
      completedInterview().turns[1],
    ],
  };
  apiMock.getInterview
    .mockReset()
    .mockImplementation(id =>
      id === 'interview-1' ? old.promise : Promise.resolve(next),
    );
  const {result, rerender} = rehydrateSession('interview-1');
  rerender({id: 'interview-2'});
  await waitFor(() => expect(result.current.interview?.id).toBe('interview-2'));
  await act(async () => {
    old.resolve({
      ...completedInterview(),
      turns: [
        {...completedInterview().turns[0], content: 'Old private transcript'},
      ],
    });
  });
  expect(result.current.interview?.id).toBe('interview-2');
  expect(result.current.messages.map(message => message.content)).toEqual([
    'Current private transcript',
  ]);
});

it('keeps a linked draft recoverable without treating it as started', async () => {
  apiMock.getInterview.mockResolvedValue({
    ...completedInterview(),
    run_id: 'run-1',
  });
  apiMock.listInterviews.mockResolvedValue([makeChat()]);
  // The linked-run lookup falls back to getRun until run history loads; without
  // this mock it rejects first and the button only appears on the later,
  // history-triggered lookup, whose timing depends on machine load.
  const draft = makeRunWithSummary({id: 'run-1', status: 'draft'}, 'chat');
  apiMock.getRun.mockResolvedValue(draft);
  apiMock.listRuns.mockResolvedValue([draft]);

  await act(async () => {
    renderWorkspace('/chats/interview-1');
  });

  expect(
    await screen.findByRole('button', {name: 'Continue research'}),
  ).toBeEnabled();
  expect(
    screen.queryByRole('region', {name: 'Started research session'}),
  ).not.toBeInTheDocument();
  expect(apiMock.startRun).not.toHaveBeenCalled();

  // Each history update re-resolves the linked run and remounts the button, so
  // click a freshly found one rather than a node captured before the remount.
  fireEvent.click(
    await screen.findByRole('button', {name: 'Continue research'}),
  );

  await waitFor(() => expect(apiMock.startRun).toHaveBeenCalledWith('run-1'));
});

it('shows no home page while a reopened chat transcript loads', async () => {
  const transcript = deferred<ReturnType<typeof completedInterview>>();
  apiMock.getInterview.mockReturnValue(transcript.promise);
  renderWorkspace('/chats/interview-1');
  expect(screen.queryByText('Frame the research goal')).toBeNull();

  await act(async () => transcript.resolve(completedInterview()));
  expect(
    await screen.findByText(
      'I have enough detail to configure this research run.',
    ),
  ).toBeInTheDocument();
});

it('falls back to the home page when a reopened chat is unavailable', async () => {
  apiMock.getInterview.mockRejectedValue(new Error('missing'));
  renderWorkspace('/chats/missing');
  expect(
    await screen.findByText('Frame the research goal'),
  ).toBeInTheDocument();
});

it('offers a jump to the latest message once the reader scrolls up', async () => {
  apiMock.getInterview.mockResolvedValue(completedInterview());
  const {container} = renderWorkspace('/chats/interview-1');
  await screen.findByText(
    'I have enough detail to configure this research run.',
  );
  const scroller = container.querySelector<HTMLElement>(
    '.reference-chat-timeline',
  )!;
  Object.defineProperty(scroller, 'scrollHeight', {value: 1000});
  Object.defineProperty(scroller, 'clientHeight', {value: 400});
  const scrollTo = vi.fn();
  scroller.scrollTo = scrollTo;
  expect(
    screen.queryByRole('button', {name: 'Jump to latest message'}),
  ).toBeNull();

  scroller.scrollTop = 100;
  fireEvent.scroll(scroller);
  fireEvent.click(
    await screen.findByRole('button', {name: 'Jump to latest message'}),
  );
  expect(scrollTo).toHaveBeenCalledWith({top: 1000, behavior: 'smooth'});

  scroller.scrollTop = 600;
  fireEvent.scroll(scroller);
  await waitFor(() =>
    expect(
      screen.queryByRole('button', {name: 'Jump to latest message'}),
    ).toBeNull(),
  );
});
