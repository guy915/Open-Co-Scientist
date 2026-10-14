import type {ReactElement} from 'react';
import {fireEvent, screen, render} from '@testing-library/react';
import {expect, it, vi, describe} from 'vitest';
import {
  buildTimelineItems,
  type BuildTimelineItemsArgs,
} from './chat_workspace_timeline';
import {makeSpec} from '@/shared/testing/fixtures';
import {makeMessage} from './chat_message_fixture';
import {MemoryRouter, type NavigateFunction} from 'react-router-dom';

describe('chat workspace timeline', () => {
  it('enables durable Q&A revisions after start and disables consumed setup', () => {
    const args = baseArgs({
      startedSession: {id: 'run-1', title: 'Research', at: 3},
      messages: [
        makeMessage({id: 'setup', turnId: 1}),
        makeMessage({id: 'qa-user', messageId: 4}),
        makeMessage({id: 'qa-answer', role: 'assistant', messageId: 5}),
      ],
    });
    renderItems(buildTimelineItems(args));
    expect(screen.getAllByRole('button', {name: 'Edit prompt'})).toHaveLength(
      1,
    );
    expect(
      screen.getAllByRole('button', {name: 'Retry response'}),
    ).toHaveLength(1);
  });

  it('renders a confirmed plan read-only, retryable, with inert actions', () => {
    const spec = makeSpec({goal: 'Confirmed goal'});
    const args = baseArgs({confirmed: {spec, createdAt: 9}});
    const items = buildTimelineItems(args);
    renderItems(items);

    expect(screen.queryByText('Cancel')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Edit plan')).not.toBeInTheDocument();
    expect(screen.getByText('Start research')).toBeDisabled();

    fireEvent.click(screen.getByLabelText('Retry response'));
    expect(args.stageDraftSpec).toHaveBeenCalledWith(spec);

    const card = items.find(item => item.id === 'confirmed-spec')!
      .node as ReactElement<Record<string, () => void>>;
    for (const inert of [
      'onFocusChange',
      'onTierChange',
      'onFieldsChange',
      'onCancel',
      'onStart',
    ]) {
      expect(() => card.props[inert]()).not.toThrow();
    }
  });

  it('places the started card right after the start request despite clock skew', () => {
    const items = buildTimelineItems(
      baseArgs({
        // The client clock runs 30 s behind the server rows.
        startedSession: {id: 'run-1', title: 'Research', at: 70},
        messages: [
          makeMessage({id: 'setup', turnId: 1, created_at: 50}),
          makeMessage({id: 'start', startRequest: true, created_at: 100}),
          makeMessage({id: 'qa', messageId: 4, created_at: 101}),
        ],
      }),
    );
    expect(items.map(item => item.id)).toEqual([
      'settled-reply',
      'local-message-setup',
      'local-message-start',
      'started-session-run-1',
      'local-message-qa',
    ]);
  });

  it('keeps bubbles in transcript order when clocks disagree', () => {
    const items = buildTimelineItems(
      baseArgs({
        isAwaitingAgent: true,
        messages: [
          makeMessage({id: 'server', created_at: 200}),
          makeMessage({id: 'client', created_at: 150}),
        ],
      }),
    );
    expect(items.map(item => item.id)).toEqual([
      'settled-reply',
      'local-message-server',
      'local-message-client',
      'agent-turn-in-flight',
    ]);
  });

  // jsdom cannot measure spacing; matching the ordinary assistant wrapper
  // guards its structure.
});

export function baseArgs(
  overrides: Partial<BuildTimelineItemsArgs> = {},
): BuildTimelineItemsArgs {
  return {
    messages: [],
    handleEditMessage: vi.fn(),
    handleCopyRequest: vi.fn().mockResolvedValue(undefined),
    handleRetryMessage: vi.fn(),
    draft: null,
    setDraft: vi.fn(),
    isStarting: false,
    isAwaitingAgent: false,
    agentReasoning: '',
    agentDraft: '',
    handleCancelDraftSpec: vi.fn(),
    handleRetryDraftSpec: vi.fn(),
    handleStartRun: vi.fn().mockResolvedValue(undefined),
    confirmed: null,
    linkedDraftRecovery: {
      canContinueLinkedDraft: false,
      status: undefined,
      retryStatusLookup: () => undefined,
    },
    stageDraftSpec: vi.fn(),
    startedSession: null,
    navigate: vi.fn() as unknown as NavigateFunction,
    resetWorkspace: vi.fn(),
    focusComposer: vi.fn(),
    ...overrides,
  };
}

export function renderItems(
  items: {id: string; node: ReactElement | unknown}[],
) {
  return render(
    <MemoryRouter>
      {items.map(item => (
        <div key={item.id}>{item.node as ReactElement}</div>
      ))}
    </MemoryRouter>,
  );
}

export function transcriptArgs(
  overrides: Partial<BuildTimelineItemsArgs> = {},
) {
  return baseArgs({
    messages: [
      makeMessage({id: 'u1', role: 'user', content: 'A question', turnId: 1}),
      makeMessage({
        id: 'a1',
        role: 'assistant',
        content: 'An answer',
        turnId: 2,
      }),
    ],
    ...overrides,
  });
}

it('keeps the announcer mounted when the optimistic first row gets a durable id', () => {
  const pending = baseArgs({
    isAwaitingAgent: true,
    messages: [makeMessage({id: 'optimistic', role: 'user'})],
  });
  const before = buildTimelineItems(pending);
  const {rerender} = render(
    <>
      {before.map(item => (
        <div key={item.id}>{item.node}</div>
      ))}
    </>,
  );
  const status = screen.getByRole('status', {name: 'Research reply'});
  expect(status).toBeEmptyDOMElement();
  const settled = buildTimelineItems(
    baseArgs({
      messages: [
        makeMessage({id: 'turn-user', role: 'user'}),
        makeMessage({
          id: 'turn-agent',
          role: 'assistant',
          turnId: 2,
          content: 'Settled interview answer',
        }),
      ],
    }),
  );
  rerender(
    <>
      {settled.map(item => (
        <div key={item.id}>{item.node}</div>
      ))}
    </>,
  );
  expect(screen.getByRole('status', {name: 'Research reply'})).toBe(status);
  expect(status).toHaveTextContent('Settled interview answer');
});

it('announces Q&A after the started card despite server/client clock skew', () => {
  const pending = baseArgs({
    isAwaitingAgent: true,
    startedSession: {id: 'run', title: 'Goal', at: 200, intro: 'Started reply'},
    messages: [
      makeMessage({id: 'setup', role: 'user', turnId: 1, created_at: 100}),
    ],
  });
  const region = (args: BuildTimelineItemsArgs) =>
    buildTimelineItems(args).find(item => item.id === 'settled-reply')!.node;
  const {rerender} = render(<>{region(pending)}</>);
  rerender(
    <>
      {region({
        ...pending,
        isAwaitingAgent: false,
        messages: [
          ...pending.messages,
          makeMessage({
            id: 'qa',
            role: 'assistant',
            messageId: 3,
            content: 'Latest Q&A reply',
            created_at: 101,
          }),
        ],
      })}
    </>,
  );
  expect(
    screen.getByRole('status', {name: 'Research reply'}),
  ).toHaveTextContent('Latest Q&A reply');
});

it('announces the settled run-start reply with status context', () => {
  const initial = baseArgs({
    startedSession: {id: 'run-1', title: 'Research', at: 1, announcing: true},
  });
  const view = render(
    <MemoryRouter>
      {buildTimelineItems(initial).map(item => (
        <div key={item.id}>{item.node}</div>
      ))}
    </MemoryRouter>,
  );
  expect(
    screen.getByRole('status', {name: 'Research reply'}),
  ).toBeEmptyDOMElement();
  const settled = baseArgs({
    startedSession: {
      id: 'run-1',
      title: 'Research',
      at: 1,
      announcing: false,
      intro: 'Your research session is ready.',
    },
  });
  view.rerender(
    <MemoryRouter>
      {buildTimelineItems(settled).map(item => (
        <div key={item.id}>{item.node}</div>
      ))}
    </MemoryRouter>,
  );
  expect(
    screen.getByRole('status', {name: 'Research reply'}),
  ).toHaveTextContent('Research started. Your research session is ready.');
});
