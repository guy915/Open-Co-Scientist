import type {Interview, QaSource} from '@/shared/api/runs';
import {makeRunMessage} from '@/shared/testing/fixtures';
import {fireEvent, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {
  apiMock,
  installChatWorkspaceMocks,
  renderWorkspace,
} from './chat_workspace_test_helpers';

const GOAL = 'Reverse liver fibrosis';
const QUESTION = 'Which mechanisms should I prioritize?';

function activeInterview(extra: Partial<Interview> = {}): Interview {
  return {
    id: 'interview-1',
    client_id: 'client-1',
    status: 'active',
    fields: {
      research_challenge: GOAL,
      focus_area: [],
      preferences: [],
      lab_constraints: [],
      title: null,
    },
    current_question: QUESTION,
    documents: [],
    turns: [
      {
        id: 1,
        role: 'user',
        content: GOAL,
        reasoning: null,
        fallback: false,
        questions: [],
        created_at: 1,
      },
      {
        id: 2,
        role: 'agent',
        content: QUESTION,
        reasoning: null,
        fallback: false,
        questions: [],
        created_at: 2,
      },
    ],
    created_at: 1,
    updated_at: 2,
    completed_at: null,
    ...extra,
  };
}

function abortWhenStopped(signal: AbortSignal | undefined) {
  return new Promise<never>((_resolve, reject) => {
    signal?.addEventListener('abort', () =>
      reject(new DOMException('The user aborted a request.', 'AbortError')),
    );
  });
}

function composer(): HTMLTextAreaElement {
  return screen
    .getAllByRole('textbox')
    .find(el => el.tagName === 'TEXTAREA') as HTMLTextAreaElement;
}

function send(text: string) {
  fireEvent.change(composer(), {target: {value: text}});
  fireEvent.click(screen.getByRole('button', {name: 'Send'}));
}

async function openInterview() {
  apiMock.createInterview.mockResolvedValue(activeInterview());
  renderWorkspace();
  send(GOAL);
  await screen.findByText(QUESTION, {selector: '.reference-model-bubble *'});
}

beforeEach(() => {
  installChatWorkspaceMocks();
  vi.clearAllMocks();
});

async function startedSession() {
  renderWorkspace();
  send(GOAL);
  await screen.findByRole('heading', {name: 'Research plan'});
  fireEvent.click(screen.getByText('Start research'));
  await screen.findByText('Research session');
}

it('keeps reasoning on a settled run answer and revises its question by durable id', async () => {
  await startedSession();
  apiMock.askRunQuestion.mockImplementation(
    async (
      _id: string,
      _q: string,
      sinks: {
        onSources?: (sources: QaSource[]) => void;
        onReasoning?: (text: string) => void;
        onChunk?: (text: string) => void;
      },
    ) => {
      sinks.onSources?.([
        {n: 1, evidence_id: 'ev-1', title: 'A paper', state: 'verified'},
      ]);
      sinks.onReasoning?.('Checking the evidence first.');
      sinks.onChunk?.('Yes, because of the evidence.');
      return 9;
    },
  );
  apiMock.getRunMessages.mockResolvedValue([
    makeRunMessage({id: 7, content: 'Does the evidence support this?'}),
    makeRunMessage({
      id: 8,
      sender: 'system',
      content: 'Yes, because of the evidence.',
    }),
  ]);
  send('Does the evidence support this?');
  await waitFor(() => {
    expect(screen.getByText('Yes, because of the evidence.')).toBeVisible();
    expect(
      screen.getByText('Checking the evidence first.'),
    ).toBeInTheDocument();
  });

  fireEvent.click(screen.getByLabelText('Edit prompt'));
  fireEvent.change(screen.getByLabelText('Edit prompt'), {
    target: {value: 'Is it reproducible?'},
  });
  fireEvent.click(screen.getByLabelText('Send edited prompt'));

  await waitFor(() =>
    expect(apiMock.askRunQuestion).toHaveBeenLastCalledWith(
      'run-1',
      'Is it reproducible?',
      expect.any(Object),
      expect.any(AbortSignal),
      7,
    ),
  );
  expect(apiMock.editInterviewTurn).not.toHaveBeenCalled();
});

it('streams the interview reply and its reasoning live, then settles on the saved turn', async () => {
  let finish!: (interview: Interview) => void;
  apiMock.createInterview.mockImplementation(
    (
      _goal: string,
      sinks: {
        onReasoning: (text: string) => void;
        onProse: (text: string) => void;
      },
    ) => {
      sinks.onReasoning('Ask about the mechanism.');
      sinks.onProse('Which mechanisms');
      return new Promise<Interview>(resolve => (finish = resolve));
    },
  );
  renderWorkspace();
  send(GOAL);
  // The lazy Markdown renderer replaces the pending plain-text node once it
  // loads, so assert against a fresh query rather than a node found earlier.
  await waitFor(() => {
    expect(screen.getByText('Ask about the mechanism.')).toBeVisible();
    expect(screen.getByText(/Which mechanisms/)).toBeInTheDocument();
  });

  finish(activeInterview());

  expect(
    await screen.findByText(QUESTION, {selector: '.reference-model-bubble *'}),
  ).toBeInTheDocument();
  expect(screen.queryByText('Ask about the mechanism.')).toBeNull();
});

it('stopping a first turn restores the composer text without a banner', async () => {
  apiMock.createInterview.mockImplementation(
    (_goal: string, _sinks: unknown, _docs: string[], signal: AbortSignal) =>
      abortWhenStopped(signal),
  );
  renderWorkspace();
  send(GOAL);

  fireEvent.click(await screen.findByRole('button', {name: 'Stop'}));

  await waitFor(() => expect(composer()).toHaveValue(GOAL));
  expect(screen.queryByRole('button', {name: 'Stop'})).toBeNull();
  expect(screen.queryByText(/aborted/i)).toBeNull();
  expect(apiMock.getInterview).not.toHaveBeenCalled();
});

it('stopping a run question drops the partial answer without a banner', async () => {
  await startedSession();
  apiMock.askRunQuestion.mockImplementation(
    (
      _id: string,
      _q: string,
      sinks: {onChunk?: (text: string) => void},
      signal: AbortSignal,
    ) => {
      sinks.onChunk?.('partial answer');
      return abortWhenStopped(signal);
    },
  );
  send('Why?');
  await screen.findByText('partial answer');

  fireEvent.click(screen.getByRole('button', {name: 'Stop'}));

  await waitFor(() => expect(screen.queryByText('partial answer')).toBeNull());
  expect(screen.getByText('Why?')).toBeInTheDocument();
  expect(screen.queryByText(/aborted/i)).toBeNull();
});

it('stopping a follow-up turn resyncs the saved interview instead of erroring', async () => {
  await openInterview();
  apiMock.addInterviewTurn.mockImplementation(
    (
      _id: string,
      _text: string,
      _s: unknown,
      _d: string[],
      signal: AbortSignal,
    ) => abortWhenStopped(signal),
  );
  const saved = activeInterview();
  saved.turns.splice(1, 0, {
    id: 3,
    role: 'user',
    content: 'one more thing',
    reasoning: null,
    fallback: false,
    questions: [],
    created_at: 3,
  });
  apiMock.getInterview.mockResolvedValue(saved);
  send('one more thing');

  fireEvent.click(await screen.findByRole('button', {name: 'Stop'}));

  await waitFor(() =>
    expect(screen.getByText('one more thing')).toBeInTheDocument(),
  );
  expect(apiMock.getInterview).toHaveBeenCalledWith('interview-1');
  expect(screen.queryByText(/aborted/i)).toBeNull();
});

it('revises a prompt in place and retries a response from the transcript', async () => {
  await openInterview();
  const revised = activeInterview({
    turns: activeInterview().turns.map(turn =>
      turn.id === 1 ? {...turn, content: 'Reverse cardiac fibrosis'} : turn,
    ),
  });
  apiMock.editInterviewTurn.mockResolvedValue(revised);
  apiMock.retryInterviewTurn.mockResolvedValue(activeInterview());
  fireEvent.change(composer(), {target: {value: 'unsent draft'}});

  fireEvent.click(screen.getByLabelText('Edit prompt'));
  fireEvent.change(screen.getByLabelText('Edit prompt'), {
    target: {value: '  Reverse cardiac fibrosis  '},
  });
  fireEvent.click(screen.getByLabelText('Send edited prompt'));

  expect(
    await screen.findByText('Reverse cardiac fibrosis'),
  ).toBeInTheDocument();
  expect(apiMock.editInterviewTurn).toHaveBeenCalledWith(
    'interview-1',
    1,
    'Reverse cardiac fibrosis',
    expect.any(Object),
    expect.any(AbortSignal),
  );
  expect(composer()).toHaveValue('unsent draft');

  fireEvent.click(screen.getAllByLabelText('Retry response').at(-1)!);
  await waitFor(() =>
    expect(apiMock.retryInterviewTurn).toHaveBeenCalledWith(
      'interview-1',
      2,
      expect.any(Object),
      expect.any(AbortSignal),
    ),
  );
});
