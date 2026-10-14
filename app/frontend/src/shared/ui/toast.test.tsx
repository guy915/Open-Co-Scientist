import {act, render, screen} from '@testing-library/react';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {Toast} from './toast';
import {EXIT_MS} from './use_presence';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

it('keeps its last message hidden through the exit, then unmounts', () => {
  const {rerender} = render(<Toast>Saved</Toast>);
  expect(screen.getByRole('status')).toHaveTextContent('Saved');

  rerender(<Toast>{null}</Toast>);
  expect(screen.queryByRole('status')).toBeNull();
  expect(screen.getByText('Saved').closest('[data-state]')).toHaveAttribute(
    'data-state',
    'closed',
  );

  act(() => {
    vi.advanceTimersByTime(EXIT_MS);
  });
  expect(screen.queryByText('Saved')).toBeNull();
});
