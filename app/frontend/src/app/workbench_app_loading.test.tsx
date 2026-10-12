import type {ReactNode} from 'react';
import {fireEvent, render, screen} from '@testing-library/react';
import {Link, MemoryRouter} from 'react-router-dom';
import {expect, it, vi} from 'vitest';
import {WorkbenchApp} from './workbench_app';

const imports = vi.hoisted(() => ({run: 0}));

vi.mock('@/shared/lib/ui_logging', () => ({logUiError: vi.fn()}));

vi.mock('@/features/report/run_detail', () => {
  imports.run += 1;
  throw new Error('The run page chunk could not be downloaded');
});
vi.mock('@/features/chat/chat_workspace', () => ({
  ChatWorkspace: () => <p>Chat home</p>,
}));
vi.mock('./layout', () => ({
  Layout: ({children}: {children: ReactNode}) => (
    <>
      <p>Persistent shell</p>
      <Link to="/runs/test-run/details">Open run</Link>
      {children}
    </>
  ),
}));
vi.mock('@/shared/hooks/theme_context', () => ({
  ThemeProvider: ({children}: {children: ReactNode}) => children,
}));
vi.mock('@/shared/hooks/system_status_context', () => ({
  SystemStatusProvider: ({children}: {children: ReactNode}) => children,
}));
vi.mock('@/shared/hooks/history_context', () => ({
  RunHistoryProvider: ({children}: {children: ReactNode}) => children,
  ChatHistoryProvider: ({children}: {children: ReactNode}) => children,
}));

it('opens home without importing the report page, then reports a failed route chunk', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  render(
    <MemoryRouter initialEntries={['/']}>
      <WorkbenchApp />
    </MemoryRouter>,
  );
  expect(screen.getByText('Chat home')).toBeVisible();
  expect(imports).toEqual({run: 0});

  fireEvent.click(screen.getByRole('link', {name: 'Open run'}));
  expect(await screen.findByText('Something went wrong')).toBeVisible();
  expect(screen.getByRole('button', {name: 'Reload Page'})).toBeEnabled();
  expect(imports).toEqual({run: 1});
  vi.restoreAllMocks();
});
