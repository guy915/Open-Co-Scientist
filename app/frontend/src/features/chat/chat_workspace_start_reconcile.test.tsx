import {act, fireEvent, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, it} from 'vitest';
import {
  ANNOUNCEMENT_TEXT,
  apiMock,
  installChatWorkspaceMocks,
  renderWorkspace,
} from './chat_workspace_test_helpers';
import {makeChat, makeRunMessage} from '@/shared/testing/fixtures';

beforeEach(() => {
  installChatWorkspaceMocks();
});

function reopenedPlan() {
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
        questions: [],
        created_at: 1,
      },
      {
        id: 2,
        role: 'agent' as const,
        content: 'I have enough detail to configure this research run.',
        reasoning: null,
        fallback: false,
        questions: [],
        created_at: 2,
      },
    ],
    created_at: 1,
    updated_at: 2,
    completed_at: 2,
    run_id: null,
  };
}

function startBubbles(container: HTMLElement): Element[] {
  return Array.from(
    container.querySelectorAll('.reference-user-bubble'),
  ).filter(bubble => bubble.textContent === 'Start research');
}

it('shows one start request when the chat list learns the run after the announcement', async () => {
  apiMock.getInterview.mockResolvedValue(reopenedPlan());
  // The list only links the run once it has started, as the server does.
  apiMock.listInterviews.mockImplementation(async () => [
    makeChat({
      id: 'interview-1',
      run_id: apiMock.startRun.mock.calls.length ? 'run-1' : null,
    }),
  ]);
  apiMock.getRunMessages.mockResolvedValue([
    makeRunMessage({id: 7, kind: 'start', content: 'Start research'}),
    makeRunMessage({
      id: 8,
      kind: 'start',
      sender: 'system',
      content: ANNOUNCEMENT_TEXT,
      created_at: 2,
    }),
  ]);
  const {container} = renderWorkspace('/chats/interview-1');

  fireEvent.click(await screen.findByRole('button', {name: 'Start research'}));
  expect(
    await screen.findByText(ANNOUNCEMENT_TEXT, {
      selector: '.reference-model-bubble *',
    }),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(apiMock.listInterviews.mock.calls.length).toBeGreaterThan(1),
  );
  // Let any saved-transcript load that the refreshed list could trigger land.
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 50));
  });

  expect(startBubbles(container)).toHaveLength(1);
});

it('still restores saved Q&A for a chat reopened after its run started', async () => {
  apiMock.getInterview.mockResolvedValue({...reopenedPlan(), run_id: 'run-1'});
  apiMock.listInterviews.mockResolvedValue([
    makeChat({id: 'interview-1', run_id: 'run-1'}),
  ]);
  apiMock.getRunMessages.mockResolvedValue([
    makeRunMessage({id: 7, kind: 'start', content: 'Start research'}),
    makeRunMessage({id: 9, content: 'Which idea leads?', created_at: 3}),
  ]);
  const {container} = renderWorkspace('/chats/interview-1');

  expect(await screen.findByText('Which idea leads?')).toBeInTheDocument();
  expect(startBubbles(container)).toHaveLength(1);
});
