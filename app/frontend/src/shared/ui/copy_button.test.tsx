import {fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {CopyButton} from './copy_button';

afterEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard');
});

function stubClipboard(writeText: ReturnType<typeof vi.fn>) {
  Object.defineProperty(navigator, 'clipboard', {
    value: {writeText},
    configurable: true,
  });
}

it('confirms a successful copy', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  stubClipboard(writeText);
  render(<CopyButton text="answer" label="Copy response" />);
  fireEvent.click(screen.getByRole('button', {name: 'Copy response'}));
  await waitFor(() =>
    expect(screen.getByRole('button', {name: 'Copied'})).toBeInTheDocument(),
  );
  expect(writeText).toHaveBeenCalledWith('answer');
});

it('does not confirm a copy that failed', async () => {
  const writeText = vi.fn().mockRejectedValue(new Error('denied'));
  stubClipboard(writeText);
  Object.defineProperty(document, 'execCommand', {
    value: () => false,
    configurable: true,
  });
  render(<CopyButton text="answer" label="Copy response" />);
  fireEvent.click(screen.getByRole('button', {name: 'Copy response'}));
  await waitFor(() => expect(writeText).toHaveBeenCalled());
  await Promise.resolve();
  expect(screen.queryByRole('button', {name: 'Copied'})).toBeNull();
  expect(
    screen.getByRole('button', {name: 'Copy response'}),
  ).toBeInTheDocument();
});
