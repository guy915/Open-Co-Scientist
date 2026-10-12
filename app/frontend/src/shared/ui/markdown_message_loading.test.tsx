import type {ComponentType} from 'react';
import {act, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {ErrorBoundary} from './error_boundary';

vi.mock('@/shared/lib/ui_logging', () => ({logUiError: vi.fn()}));

interface RendererModule {
  MarkdownMessageRenderer: ComponentType<{
    content: string;
    className?: string;
  }>;
}

function pendingRenderer() {
  let resolve!: (module: RendererModule) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<RendererModule>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return {resolve, reject, load: vi.fn(() => promise)};
}

afterEach(() => {
  vi.doUnmock('./markdown_message_renderer');
  vi.restoreAllMocks();
});

it('keeps the shell and current escaped text readable when formatting cannot load', async () => {
  vi.resetModules();
  vi.spyOn(console, 'error').mockImplementation(() => {});
  const renderer = pendingRenderer();
  vi.doMock('./markdown_message_renderer', renderer.load);
  const {MarkdownMessage} = await import('./markdown_message');
  const view = render(
    <ErrorBoundary>
      <p>Persistent chat shell</p>
      <MarkdownMessage content="Readable pending reply" />
    </ErrorBoundary>,
  );
  expect(screen.getByText('Readable pending reply')).toBeVisible();
  await waitFor(() => expect(renderer.load).toHaveBeenCalledOnce());
  await act(async () => {
    renderer.reject(new Error('The Markdown chunk could not be downloaded'));
  });
  expect(await screen.findByRole('status')).toHaveTextContent(
    'Formatting unavailable.',
  );
  expect(screen.getByText('Persistent chat shell')).toBeVisible();
  expect(screen.getByText('Readable pending reply')).toBeVisible();
  expect(screen.queryByText('Something went wrong')).toBeNull();
  const unsafe = '<img src=x onerror="alert(1)"> latest reply';
  view.rerender(
    <ErrorBoundary>
      <p>Persistent chat shell</p>
      <MarkdownMessage content={unsafe} />
    </ErrorBoundary>,
  );
  expect(screen.getByText(unsafe)).toBeVisible();
  expect(view.container.querySelector('img')).toBeNull();
  expect(renderer.load).toHaveBeenCalledOnce();
  expect(screen.getByRole('button', {name: 'Reload page'})).toBeEnabled();
});
