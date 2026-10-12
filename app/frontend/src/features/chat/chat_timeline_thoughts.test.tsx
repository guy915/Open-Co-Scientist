import {render, screen} from '@testing-library/react';
import {expect, test} from 'vitest';
import {ThoughtsDisclosure} from './chat_timeline_thoughts';

// Decorative dots are aria-hidden, so query their class rather than their role.
const DOTS = '.reference-thinking-dots';

function isOpen(container: HTMLElement): boolean {
  return (
    container.querySelector('button')?.getAttribute('aria-expanded') === 'true'
  );
}

test('counts dots and stands open while the turn is still being written', () => {
  const {container} = render(
    <ThoughtsDisclosure reasoning="Weighing two mechanisms." live />,
  );

  expect(container.querySelector(DOTS)).not.toBeNull();
  expect(isOpen(container)).toBe(true);
  expect(screen.getByText('Weighing two mechanisms.')).toBeInTheDocument();
  expect(container.querySelector(DOTS)?.textContent).toBe('...');
});

test('drops the dots and closes once the turn has landed', () => {
  const {container} = render(
    <ThoughtsDisclosure reasoning="Weighing two mechanisms." />,
  );

  expect(screen.getByText('Thinking')).toBeInTheDocument();
  expect(container.querySelector(DOTS)).toBeNull();
  expect(isOpen(container)).toBe(false);
});

test('folds itself away as soon as the answer starts arriving', () => {
  // The first reply token makes thinking stale; waiting until completion buries
  // the answer.
  const {container, rerender} = render(
    <ThoughtsDisclosure reasoning="A thought." live />,
  );
  expect(isOpen(container)).toBe(true);

  rerender(<ThoughtsDisclosure reasoning="A thought." live answering />);

  expect(isOpen(container)).toBe(false);
});

test('announces the live label only, never the raw chain of thought', () => {
  render(<ThoughtsDisclosure reasoning="A thought." live />);

  expect(
    screen.getByText('Thinking').closest('[role="status"]'),
  ).not.toBeNull();
  // Live announcements would reread reasoning on every token.
  expect(screen.getByText('A thought.').closest('[aria-live]')).toBeNull();
});
