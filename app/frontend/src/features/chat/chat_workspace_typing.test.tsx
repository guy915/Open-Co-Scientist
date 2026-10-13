import type {Interview} from '@/shared/api/runs';
import {act, fireEvent, screen} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import {
  apiMock,
  installChatWorkspaceMocks,
  renderWorkspace,
} from './chat_workspace_test_helpers';

const GOAL = 'Reverse liver fibrosis';
const QUESTION = 'Which mechanisms should I prioritize?';

const bubbleRenders = vi.hoisted(() => ({count: 0}));

vi.mock('./chat_timeline_bubble', async importOriginal => {
  const actual =
    await importOriginal<typeof import('./chat_timeline_bubble')>();
  return {
    ...actual,
    ChatBubble: (props: Parameters<typeof actual.ChatBubble>[0]) => {
      bubbleRenders.count += 1;
      return <actual.ChatBubble {...props} />;
    },
  };
});

function activeInterview(goal: string): Interview {
  return {
    id: 'interview-1',
    client_id: 'client-1',
    status: 'active',
    fields: {
      research_challenge: goal,
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
        content: goal,
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
  };
}

function composer(): HTMLTextAreaElement {
  return screen
    .getAllByRole('textbox')
    .find(el => el.tagName === 'TEXTAREA') as HTMLTextAreaElement;
}

beforeEach(() => {
  installChatWorkspaceMocks();
  vi.clearAllMocks();
});

it('does not re-render transcript bubbles while the composer is typed in', async () => {
  apiMock.createInterview.mockImplementation(async (goal: string) =>
    activeInterview(goal),
  );
  renderWorkspace();
  fireEvent.change(composer(), {target: {value: GOAL}});
  fireEvent.click(screen.getByRole('button', {name: 'Send'}));
  await screen.findByText(QUESTION, {selector: '.reference-model-bubble *'});
  // Sending re-renders the transcript once more as the new chat settles.
  await act(() => new Promise(resolve => setTimeout(resolve, 50)));

  bubbleRenders.count = 0;
  for (const text of ['M', 'Mi', 'Mit', 'Mito']) {
    fireEvent.change(composer(), {target: {value: text}});
  }

  expect(composer().value).toBe('Mito');
  expect(bubbleRenders.count).toBe(0);
});
