import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {useRef, useState} from 'react';
import {expect, it, vi} from 'vitest';
import {Dialog} from './dialog';

vi.mock('@/shared/lib/ui_logging', () => ({logModalOpen: vi.fn()}));

function Harness({onSubmit}: {onSubmit?: () => void}) {
  const [open, setOpen] = useState(false);
  const fieldRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <main>
        <button type="button" onClick={() => setOpen(true)}>
          Open
        </button>
      </main>
      <Dialog
        open={open}
        onClose={() => setOpen(false)}
        label="Example"
        initialFocusRef={fieldRef}
        onSubmit={
          onSubmit &&
          (event => {
            event.preventDefault();
            onSubmit();
          })
        }
      >
        <input ref={fieldRef} aria-label="Field" />
        <button type="button" onClick={() => setOpen(false)}>
          Done
        </button>
      </Dialog>
    </>
  );
}

it('focuses the initial field and makes the page behind inert', () => {
  const {container} = render(<Harness />);
  fireEvent.click(screen.getByRole('button', {name: 'Open'}));
  expect(screen.getByRole('dialog', {name: 'Example'})).toHaveAttribute(
    'aria-modal',
    'true',
  );
  expect(screen.getByLabelText('Field')).toHaveFocus();
  expect(container).toHaveAttribute('inert');
});

it('closes on Escape, restores focus at once and fades out before unmounting', async () => {
  const {container} = render(<Harness />);
  const opener = screen.getByRole('button', {name: 'Open'});
  opener.focus();
  fireEvent.click(opener);
  act(() => {
    fireEvent.keyDown(window, {key: 'Escape'});
  });
  // Focus and the background return before the fade ends; only the visuals
  // linger.
  expect(container).not.toHaveAttribute('inert');
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(opener).toHaveFocus());
  expect(document.querySelector('[data-state="closed"]')).not.toBeNull();
  await waitFor(() =>
    expect(document.querySelector('[data-state="closed"]')).toBeNull(),
  );
});

it('closes from the scrim', () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole('button', {name: 'Open'}));
  const scrim = document.querySelector('.bg-scrim');
  fireEvent.click(scrim!);
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('renders a form panel when the dialog submits', () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  fireEvent.click(screen.getByRole('button', {name: 'Open'}));
  const dialog = screen.getByRole('dialog', {name: 'Example'});
  expect(dialog.tagName).toBe('FORM');
  fireEvent.submit(dialog);
  expect(onSubmit).toHaveBeenCalledTimes(1);
});
