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

// The questions show as soon as the live turn has written them, before the
// turn is saved; Send waits for the save, and selections survive it.
it('shows a live turn questions early and keeps the pick once it is saved', async () => {
  let finish: (interview: Interview) => void = () => undefined;
  apiMock.createInterview.mockImplementation(
    (
      _text: string,
      sinks: {
        onProse?: (fragment: string) => void;
        onQuestions?: (questions: InterviewQuestion[]) => void;
      } = {},
    ) => {
      sinks.onProse?.('A few directions are worth separating here.');
      sinks.onQuestions?.([MODEL_SYSTEM]);
      return new Promise<Interview>(resolve => {
        finish = resolve;
      });
    },
  );
  renderWorkspace();
  fireEvent.change(screen.getByRole('textbox'), {
    target: {value: 'Reverse cardiac fibrosis'},
  });
  fireEvent.click(screen.getByRole('button', {name: 'Send'}));

  await screen.findByText(MODEL_SYSTEM.question);
  fireEvent.click(screen.getByLabelText(/iPSC-derived line/));
  expect(screen.getByRole('button', {name: /send answer/i})).toBeDisabled();

  finish(asking([MODEL_SYSTEM]));

  await waitFor(() => {
    expect(screen.getByRole('button', {name: /send answer/i})).toBeEnabled();
  });
  expect(screen.getByLabelText(/iPSC-derived line/)).toBeChecked();
});
