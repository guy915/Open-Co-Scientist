import {fireEvent, render, screen} from '@testing-library/react';
import {expect, test, vi} from 'vitest';
import {Composer} from './chat_composer';

function renderComposer(
  overrides: Partial<Parameters<typeof Composer>[0]> = {},
) {
  const props = {
    input: '',
    setInput: vi.fn(),
    busy: false,
    onSubmit: vi.fn(e => e.preventDefault()),
    ...overrides,
  };
  render(<Composer {...props} />);
  return props;
}

test('submits on Enter without shift', () => {
  const {onSubmit} = renderComposer({input: 'a goal'});
  fireEvent.keyDown(screen.getByRole('textbox'), {key: 'Enter'});
  expect(onSubmit).toHaveBeenCalledOnce();
});

test('inserts a newline on Shift+Enter instead of submitting', () => {
  const {onSubmit} = renderComposer({input: 'a goal'});
  fireEvent.keyDown(screen.getByRole('textbox'), {
    key: 'Enter',
    shiftKey: true,
  });
  expect(onSubmit).not.toHaveBeenCalled();
});

test('blocks only submitting while the session is busy', () => {
  renderComposer({input: 'a goal', busy: true});
  expect(screen.getByRole('textbox')).toBeEnabled();
  expect(
    screen.getByRole('button', {name: /send|start|research/i}),
  ).toBeDisabled();
});

test('shows a Stop control instead of Send while awaiting the agent', () => {
  const onStop = vi.fn();
  renderComposer({input: 'a goal', busy: true, stoppable: true, onStop});
  expect(
    screen.queryByRole('button', {name: /send|start|research/i}),
  ).not.toBeInTheDocument();
  const stop = screen.getByRole('button', {name: 'Stop'});
  expect(stop).toBeEnabled();
});

test('Stop calls onStop and does not submit the form', () => {
  const onStop = vi.fn();
  const {onSubmit} = renderComposer({
    input: 'a goal',
    busy: true,
    stoppable: true,
    onStop,
  });
  fireEvent.click(screen.getByRole('button', {name: 'Stop'}));
  expect(onStop).toHaveBeenCalledOnce();
  expect(onSubmit).not.toHaveBeenCalled();
});

test('asks for a research goal first, then for the next message', () => {
  renderComposer();
  expect(screen.getByRole('textbox')).toHaveAttribute(
    'placeholder',
    'Start a new research goal to begin',
  );
});

test('asks Co-Scientist once the conversation has a message', () => {
  renderComposer({inConversation: true});
  const textbox = screen.getByRole('textbox', {name: 'Ask Co-Scientist'});
  expect(textbox).toHaveAttribute('placeholder', 'Ask Co-Scientist');
  // The placeholder is the field's own, so it starts where the caret does.
  expect(textbox.parentElement!.querySelector('svg')).toBeNull();
});
