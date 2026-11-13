import {makeQuestion} from '@/shared/testing/fixtures';
import {fireEvent, screen, waitFor} from '@testing-library/react';
import {beforeEach, expect, it, vi} from 'vitest';
import type {Interview, InterviewQuestion} from '@/shared/api/runs';
import {
  apiMock,
  installChatWorkspaceMocks,
  renderWorkspace,
} from './chat_workspace_test_helpers';

const MODEL_SYSTEM: InterviewQuestion = makeQuestion();

function asking(questions: InterviewQuestion[]): Interview {
  return {
    id: 'interview-1',
    client_id: 'scientist-a',
    status: 'active',
    fields: {
      research_challenge: 'Reverse cardiac fibrosis',
      focus_area: [],
      preferences: [],
      lab_constraints: [],
      title: null,
    },
    current_question: MODEL_SYSTEM.question,
    documents: [],
    turns: [
      {
        id: 1,
        role: 'user',
        content: 'Reverse cardiac fibrosis',
        reasoning: null,
        fallback: false,
        questions: [],
        created_at: 1,
      },
      {
        id: 2,
        role: 'agent',
        content: 'A few directions are worth separating here.',
        reasoning: null,
        fallback: false,
        questions,
        created_at: 2,
      },
    ],
    created_at: 1,
    updated_at: 2,
    completed_at: null,
  };
}

async function askTheScientist(questions = [MODEL_SYSTEM]) {
  apiMock.createInterview.mockResolvedValue(asking(questions));
  apiMock.addInterviewTurn.mockResolvedValue(asking([]));
  renderWorkspace();
  fireEvent.change(screen.getByRole('textbox'), {
    target: {value: 'Reverse cardiac fibrosis'},
  });
  fireEvent.click(screen.getByRole('button', {name: 'Send'}));
  return screen.findByText(MODEL_SYSTEM.question);
}

beforeEach(() => {
  installChatWorkspaceMocks();
  vi.clearAllMocks();
});

it('sends a clicked answer as the scientist own next turn', async () => {
  await askTheScientist();
  fireEvent.click(screen.getByLabelText(/iPSC-derived line/));
  fireEvent.click(screen.getByRole('button', {name: /send answer/i}));
  await waitFor(() => {
    expect(apiMock.addInterviewTurn).toHaveBeenCalledWith(
      'interview-1',
      'Model system: iPSC-derived line',
      expect.anything(),
      [],
      expect.anything(),
    );
  });
});
