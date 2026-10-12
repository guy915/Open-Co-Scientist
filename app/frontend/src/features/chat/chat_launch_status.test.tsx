import {fireEvent, render, screen} from '@testing-library/react';
import {expect, it, vi} from 'vitest';
import {LaunchStatusContext} from '@/shared/hooks/launch_status_context';
import type {LaunchStatus} from '@/shared/api/launch_control';
import {Composer} from '@/features/chat/chat_composer';
import {LaunchStatusBanner} from '@/shared/ui/launch_status_banner';

const paused: LaunchStatus = {
  reason: 'paused',
  message: 'Research is paused while we recover the service.',
  resumes_at: null,
  paused: true,
  free_runs_allowed: false,
  byok_runs_allowed: false,
};

it('updates one persistent live region and explains the disabled send action', () => {
  const submit = vi.fn();
  const view = (status: LaunchStatus | null) => (
    <LaunchStatusContext.Provider value={status}>
      <LaunchStatusBanner />
      <Composer
        input="Synthetic question"
        setInput={vi.fn()}
        busy={false}
        onSubmit={submit}
      />
    </LaunchStatusContext.Provider>
  );
  const {rerender} = render(view(null));
  const region = screen.getByRole('status');
  rerender(view(paused));
  expect(screen.getByRole('status')).toBe(region);
  expect(region).toHaveTextContent(paused.message!);
  expect(screen.getByRole('button', {name: 'Send'})).toBeDisabled();
  fireEvent.keyDown(screen.getByRole('textbox'), {key: 'Enter'});
  fireEvent.submit(screen.getByRole('textbox').closest('form')!);
  expect(submit).not.toHaveBeenCalled();
  rerender(
    view({
      ...paused,
      paused: false,
      free_runs_allowed: true,
      byok_runs_allowed: true,
      message: null,
    }),
  );
  expect(region).toBeEmptyDOMElement();
  expect(screen.getByRole('button', {name: 'Send'})).toBeEnabled();
});

it('keeps stopping available during a pause', () => {
  const stop = vi.fn();
  render(
    <LaunchStatusContext.Provider value={paused}>
      <Composer
        input=""
        setInput={vi.fn()}
        busy
        stoppable
        onStop={stop}
        onSubmit={vi.fn()}
      />
    </LaunchStatusContext.Provider>,
  );
  fireEvent.click(screen.getByRole('button', {name: 'Stop'}));
  expect(stop).toHaveBeenCalledOnce();
});
