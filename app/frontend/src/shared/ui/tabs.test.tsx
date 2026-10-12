import {fireEvent, render, screen} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {expect, it, vi} from 'vitest';
import {SegmentedControl, TabNav, TabNavLink} from './tabs';

it('presses exactly the selected segment, shows its thumb and reports a new choice', () => {
  const onChange = vi.fn();
  const {container} = render(
    <SegmentedControl
      label="Theme"
      value="light"
      onChange={onChange}
      options={[
        {value: 'system', label: 'System'},
        {value: 'light', label: 'Light'},
        {value: 'dark', label: 'Dark'},
      ]}
    />,
  );
  expect(screen.getByRole('group', {name: 'Theme'})).toBeInTheDocument();
  expect(screen.getByRole('button', {name: 'Light'})).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(screen.getByRole('button', {name: 'Dark'})).toHaveAttribute(
    'aria-pressed',
    'false',
  );
  expect(container.querySelector('.ui-sliding-thumb')).not.toBeNull();
  fireEvent.click(screen.getByRole('button', {name: 'Dark'}));
  expect(onChange).toHaveBeenCalledWith('dark');
});

it('marks the current route link as the page and shows its indicator', () => {
  const {container} = render(
    <MemoryRouter>
      <TabNav label="Sections" variant="underline" current="b">
        <TabNavLink variant="underline" to="/a" current={false}>
          A
        </TabNavLink>
        <TabNavLink variant="underline" to="/b" current>
          B
        </TabNavLink>
      </TabNav>
    </MemoryRouter>,
  );
  expect(
    screen.getByRole('navigation', {name: 'Sections'}),
  ).toBeInTheDocument();
  expect(screen.getByRole('link', {name: 'B'})).toHaveAttribute(
    'aria-current',
    'page',
  );
  expect(screen.getByRole('link', {name: 'A'})).not.toHaveAttribute(
    'aria-current',
  );
  expect(container.querySelector('.ui-sliding-thumb')).not.toBeNull();
});
