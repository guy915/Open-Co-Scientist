import {act, fireEvent, render, screen} from '@testing-library/react';
import {useRef, useState} from 'react';
import {expect, it, vi} from 'vitest';
import {Menu, MenuItem} from './menu';

function Harness({onEscapeOutside}: {onEscapeOutside?: () => void}) {
  const [open, setOpen] = useState(true);
  const triggerRef = useRef<HTMLButtonElement>(null);
  return (
    <div
      onKeyDown={event => {
        if (event.key === 'Escape') onEscapeOutside?.();
      }}
    >
      <button ref={triggerRef} type="button" onClick={() => setOpen(o => !o)}>
        Trigger
      </button>
      <p>Outside</p>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        label="Options"
        anchorRefs={[triggerRef]}
        layoutClassName="absolute"
      >
        <MenuItem>First</MenuItem>
        <MenuItem kind="radio" checked>
          Second
        </MenuItem>
        <MenuItem disabled>Disabled</MenuItem>
        <MenuItem kind="checkbox">Third</MenuItem>
      </Menu>
    </div>
  );
}

it('moves focus between enabled items with the arrow keys', () => {
  render(<Harness />);
  const menu = screen.getByRole('menu', {name: 'Options'});
  expect(screen.getByRole('menuitemradio', {name: 'Second'})).toHaveFocus();
  fireEvent.keyDown(menu, {key: 'Home'});
  expect(screen.getByRole('menuitem', {name: 'First'})).toHaveFocus();
  fireEvent.keyDown(menu, {key: 'End'});
  fireEvent.keyDown(menu, {key: 'ArrowDown'});
  expect(screen.getByRole('menuitem', {name: 'First'})).toHaveFocus();
  fireEvent.keyDown(menu, {key: 'ArrowDown'});
  expect(screen.getByRole('menuitemradio', {name: 'Second'})).toHaveFocus();
  fireEvent.keyDown(menu, {key: 'ArrowDown'});
  expect(screen.getByRole('menuitemcheckbox', {name: 'Third'})).toHaveFocus();
  fireEvent.keyDown(menu, {key: 'ArrowDown'});
  expect(screen.getByRole('menuitem', {name: 'First'})).toHaveFocus();
  fireEvent.keyDown(menu, {key: 'End'});
  expect(screen.getByRole('menuitemcheckbox', {name: 'Third'})).toHaveFocus();
});

it('returns focus to the opener when Tab dismisses the menu', () => {
  render(<Harness />);
  fireEvent.keyDown(screen.getByRole('menuitemradio', {name: 'Second'}), {
    key: 'Tab',
  });
  expect(screen.queryByRole('menu')).toBeNull();
  expect(screen.getByRole('button', {name: 'Trigger'})).toHaveFocus();
});

it('marks radio and checkbox items checked', () => {
  render(<Harness />);
  expect(screen.getByRole('menuitemradio', {name: 'Second'})).toHaveAttribute(
    'aria-checked',
    'true',
  );
  expect(screen.getByRole('menuitemcheckbox', {name: 'Third'})).toHaveAttribute(
    'aria-checked',
    'false',
  );
});

it('closes on an outside press but not on the trigger or itself', () => {
  render(<Harness />);
  fireEvent.pointerDown(screen.getByRole('menuitem', {name: 'First'}));
  expect(screen.getByRole('menu')).toBeInTheDocument();
  fireEvent.pointerDown(screen.getByText('Outside'));
  expect(screen.queryByRole('menu')).toBeNull();
});

it('consumes Escape so an enclosing dialog stays open', () => {
  const outer = vi.fn();
  const windowListener = vi.fn();
  window.addEventListener('keydown', windowListener);
  render(<Harness onEscapeOutside={outer} />);
  act(() => {
    fireEvent.keyDown(screen.getByRole('menuitem', {name: 'First'}), {
      key: 'Escape',
    });
  });
  expect(screen.queryByRole('menu')).toBeNull();
  expect(windowListener).not.toHaveBeenCalled();
  window.removeEventListener('keydown', windowListener);
});
