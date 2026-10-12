import '@/shared/ui/markdown_message_renderer';
import {fireEvent, screen, render} from '@testing-library/react';
import {afterEach, expect, test, vi, beforeEach, describe} from 'vitest';
import {
  FALLBACK_NOTICE_TEXT,
  ChatBubble,
  type ChatEntry,
} from './chat_timeline_bubble';
import {makeMessage} from './chat_message_fixture';

describe('chat timeline bubble', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  test('renders an assistant message with the response action row', () => {
    const {onRetry} = renderBubble({
      role: 'assistant',
      content: 'A short reply.',
    });
    expect(screen.getByText('A short reply.')).toBeInTheDocument();
    expect(screen.getByLabelText('Retry response')).toBeInTheDocument();
    expect(screen.getByLabelText('Copy response')).toBeInTheDocument();
    expect(screen.getByLabelText('Download response')).toBeInTheDocument();
    expect(screen.queryByLabelText('Edit prompt')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Expand')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Retry response'));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  test('shows the fallback notice on a scripted assistant turn (A16)', () => {
    renderBubble({
      role: 'assistant',
      content: 'Which scientific mechanisms should this research prioritize?',
      fallback: true,
    });
    expect(screen.getByText(FALLBACK_NOTICE_TEXT)).toBeInTheDocument();
  });

  test('renders assistant markdown, and leaves user text literal', async () => {
    // Scientist text remains literal while assistant output renders Markdown.
    renderBubble({role: 'assistant', content: 'Use **primary** cells'});
    expect((await screen.findByText('primary')).tagName).toBe('STRONG');

    renderBubble({role: 'user', content: 'Use **primary** cells'});
    expect(screen.getByText('Use **primary** cells')).toBeInTheDocument();
  });
});

describe('chat timeline bubble collapse', () => {
  const longContent = 'Long line of chat text. '.repeat(40);

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    // jsdom heights are zero; provide overflowing geometry to expose the
    // toggle.
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      value: 500,
    });
  });

  afterEach(() => {
    Reflect.deleteProperty(HTMLElement.prototype, 'scrollHeight');
  });

  test('toggles expand/collapse under prefers-reduced-motion', () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({matches: true}) as unknown as (
        query: string,
      ) => MediaQueryList,
    );
    const {container} = renderBubble({content: longContent});

    const textSpan = container.querySelector('span')!;
    expect(textSpan.className).toContain('whitespace-normal');

    const expandButton = screen.getByLabelText('Expand');
    const blurSpy = vi.spyOn(expandButton, 'blur');

    fireEvent.click(expandButton, {detail: 1});

    expect(blurSpy).toHaveBeenCalledOnce();
    const collapseButton = screen.getByLabelText('Collapse');
    expect(textSpan.className).toContain('whitespace-pre-wrap');
    expect(textSpan.style.maxHeight).toBe('');

    fireEvent.click(collapseButton, {detail: 0});
    expect(blurSpy).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('Expand')).toBeInTheDocument();
    expect(textSpan.className).toContain('whitespace-normal');
  });
});

export function renderBubble(
  overrides: Partial<ChatEntry> = {},
  revisable = true,
) {
  const onSubmitEdit = vi.fn();
  const onCopyRequest = vi.fn();
  const onRetry = vi.fn();
  const message = makeMessage({content: 'Hello there', ...overrides});
  const utils = render(
    <ChatBubble
      message={message}
      revisable={revisable}
      onSubmitEdit={onSubmitEdit}
      onCopyRequest={onCopyRequest}
      onRetry={onRetry}
    />,
  );
  return {...utils, onSubmitEdit, onCopyRequest, onRetry, message};
}
