import {makeQuestion} from '@/shared/testing/fixtures';
import {fireEvent, render, screen} from '@testing-library/react';
import {describe, expect, test, vi} from 'vitest';
import {type InterviewQuestion} from '@/shared/api/runs';
import {
  QuestionChooser,
  answerText,
  emptySelections,
  toggleOption,
} from './chat_questions';

const MODEL_SYSTEM: InterviewQuestion = makeQuestion();

const EXCLUSIONS: InterviewQuestion = {
  header: 'Exclusions',
  question: 'Which directions should be excluded?',
  multi_select: true,
  options: [
    {label: 'Gene therapy', description: ''},
    {label: 'Devices', description: ''},
  ],
};

function renderChooser(
  questions: InterviewQuestion[] = [MODEL_SYSTEM],
  overrides: Partial<Parameters<typeof QuestionChooser>[0]> = {},
) {
  const props = {questions, onAnswer: vi.fn(), ...overrides};
  render(<QuestionChooser {...props} />);
  return props;
}

test('asks the question and lays out every answer with its description', () => {
  renderChooser();
  expect(screen.getByText(MODEL_SYSTEM.question)).toBeTruthy();
  expect(screen.getByLabelText(/Primary human cells/)).toBeTruthy();
  expect(screen.getByText('Closest to patient biology')).toBeTruthy();
});

test('the send control commits a single-select answer once clicked', () => {
  const {onAnswer} = renderChooser();
  fireEvent.click(screen.getByLabelText(/iPSC-derived line/));
  fireEvent.click(screen.getByRole('button', {name: /send/i}));
  expect(onAnswer).toHaveBeenCalledWith('Model system: iPSC-derived line');
});

test('a multi-select question waits for the send control', () => {
  const {onAnswer} = renderChooser([EXCLUSIONS]);
  fireEvent.click(screen.getByLabelText(/Gene therapy/));
  fireEvent.click(screen.getByLabelText(/Devices/));
  expect(onAnswer).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: /send/i}));
  expect(onAnswer).toHaveBeenCalledWith('Exclusions: Gene therapy, Devices');
});

test('one send in the head answers every question, outside their fields', () => {
  const {onAnswer} = renderChooser([MODEL_SYSTEM, EXCLUSIONS]);
  const send = screen.getByRole('button', {name: 'Send answer'});
  expect(send.closest('fieldset')).toBeNull();
  expect(send).toHaveProperty('disabled', true);
  fireEvent.click(screen.getByLabelText(/iPSC-derived line/));
  fireEvent.click(screen.getByLabelText(/Devices/));
  fireEvent.click(screen.getByRole('button', {name: 'Minimize the questions'}));
  fireEvent.click(send);
  expect(onAnswer).toHaveBeenCalledWith(
    'Model system: iPSC-derived line\nExclusions: Devices',
  );
});

describe('answer state and pending questions', () => {
  const EXCLUSIONS: InterviewQuestion = {
    header: '',
    question: 'Which directions should be excluded?',
    multi_select: true,
    options: [
      {label: 'Gene therapy', description: ''},
      {label: 'Small molecules', description: ''},
      {label: 'Devices', description: ''},
    ],
  };

  test('a multi-select question accumulates answers and clicking again removes one', () => {
    let selections = toggleOption(emptySelections(), 0, 'Gene therapy', true);
    selections = toggleOption(selections, 0, 'Devices', true);
    selections = toggleOption(selections, 0, 'Gene therapy', true);
    expect(answerText([EXCLUSIONS], selections)).toBe('Devices');
  });
});
