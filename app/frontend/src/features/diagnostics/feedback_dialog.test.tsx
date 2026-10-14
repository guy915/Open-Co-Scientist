import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {beforeEach, expect, it, vi} from 'vitest';
import {submitFeedback} from '@/shared/api/feedback';
import {postAppLogs} from '@/shared/api/logs';
import {sessionDiagnosticExport} from './diagnostics';
import {FeedbackControl} from './feedback_dialog';

vi.mock('@/shared/api/feedback', async importOriginal => ({
  ...(await importOriginal<typeof import('@/shared/api/feedback')>()),
  submitFeedback: vi.fn(),
}));
vi.mock('@/shared/api/logs', () => ({
  postAppLogs: vi.fn().mockResolvedValue({added: 1, last_id: 1}),
}));
vi.mock('./diagnostics', () => ({sessionDiagnosticExport: vi.fn()}));

beforeEach(() => {
  vi.mocked(submitFeedback)
    .mockReset()
    .mockResolvedValue({id: 'feedback-id', status: 'submitted'});
  vi.mocked(sessionDiagnosticExport)
    .mockReset()
    .mockResolvedValue('private diagnostic export');
  vi.mocked(postAppLogs).mockClear();
});

function open(path = '/') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <FeedbackControl
        runId={path.startsWith('/chats/') ? 'linked-run' : undefined}
      />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole('button', {name: 'Feedback'}));
  return screen.getByRole('dialog', {name: 'Feedback'});
}

it.each(['/runs/direct-run/details', '/chats/private-chat'])(
  'silently attaches diagnostics and run context from %s',
  async path => {
    const dialog = open(path);
    fireEvent.click(within(dialog).getByRole('button', {name: /^Category/}));
    fireEvent.click(
      within(dialog).getByRole('menuitemradio', {name: 'Results quality'}),
    );
    fireEvent.change(within(dialog).getByLabelText('Message'), {
      target: {value: '  The evidence needs a clearer explanation.  '},
    });
    fireEvent.click(within(dialog).getByRole('button', {name: 'Submit'}));
    await waitFor(() =>
      expect(submitFeedback).toHaveBeenCalledWith(
        expect.objectContaining({
          category: 'Results quality',
          message: 'The evidence needs a clearer explanation.',
          diagnostics: 'private diagnostic export',
          run_id: path.startsWith('/runs/') ? 'direct-run' : 'linked-run',
          url: window.location.href,
        }),
      ),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByText('private diagnostic export')).toBeNull();
  },
);

it('does not submit after cancelling while diagnostic collection is pending', async () => {
  let release!: (value: string) => void;
  vi.mocked(sessionDiagnosticExport).mockImplementation(
    () =>
      new Promise(resolve => {
        release = resolve;
      }),
  );
  const dialog = open();
  fireEvent.change(within(dialog).getByLabelText('Message'), {
    target: {value: 'Cancel this report'},
  });
  fireEvent.click(within(dialog).getByRole('button', {name: 'Submit'}));
  fireEvent.click(within(dialog).getByRole('button', {name: 'Cancel'}));
  await act(async () => {
    release('late logs');
    await Promise.resolve();
  });
  expect(submitFeedback).not.toHaveBeenCalled();
});
