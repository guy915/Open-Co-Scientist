import {render, screen} from '@testing-library/react';
import {expect, it} from 'vitest';
import {Chip} from './chip';

it('draws card metadata as small-radius tags and labels as pills', () => {
  render(
    <>
      <Chip shape="tag">Oct 7, 2026</Chip>
      <Chip>Offline mode</Chip>
    </>,
  );
  const tag = screen.getByText('Oct 7, 2026');
  expect(tag.className).toContain('rounded-md');
  expect(tag.className).not.toContain('rounded-full');
  expect(screen.getByText('Offline mode').className).toContain('rounded-full');
});

it('sizes a large chip like a small button so status sits level with header actions', () => {
  render(<Chip size="lg">Offline mode</Chip>);
  const chip = screen.getByText('Offline mode');
  expect(chip.className).toContain('h-[2.35rem]');
  expect(chip.className).toContain('text-[0.875rem]');
  expect(chip.className).toContain('px-3');
});
