import {render, screen, fireEvent, waitFor} from '@testing-library/react';
import {describe, expect, it, afterEach, vi} from 'vitest';
import {
  MarkdownMessageRenderer as MarkdownMessage,
  splitMarkdownIntoBlocks,
} from './markdown_message_renderer';

describe('markdown message', () => {
  describe('MarkdownMessage', () => {
    it.each([
      'https://tracker.example/pixel?private=research',
      '/api/private-image',
      '//tracker.example/pixel',
    ])('renders image alt text and URL without a request: %s', src => {
      const {container} = render(
        <MarkdownMessage content={`![Study diagram](${src})`} />,
      );
      expect(container.querySelector('img')).toBeNull();
      expect(container.querySelector('a')).toBeNull();
      expect(screen.getByText(`Study diagram (${src})`)).toBeVisible();
    });

    it('opens links in a new tab without handing over the opener', () => {
      render(<MarkdownMessage content="[PubMed](https://pubmed.gov)" />);

      const link = screen.getByRole('link', {name: 'PubMed'});
      expect(link).toHaveAttribute('href', 'https://pubmed.gov');
      expect(link).toHaveAttribute('target', '_blank');
      // Citation links must preserve the live workspace and withhold
      // opener/referrer access.
      expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    });

    it('does not render raw HTML embedded in model output', () => {
      // Model output is untrusted; raw HTML must remain inert text.
      render(
        <MarkdownMessage content={'<img src=x onerror="alert(1)"> and text'} />,
      );

      expect(document.querySelector('img')).toBeNull();
      expect(screen.getByText(/and text/)).toBeInTheDocument();
    });
  });
});

describe('markdown message blocks', () => {
  // Whole-message parsing adds inter-block whitespace that normal CSS
  // collapses; normalize it away.
  const normalize = (html: string) => html.replace(/>\s+</g, '><').trim();

  describe('MarkdownMessage per-block splitting', () => {
    it('renders identical DOM for a multi-block message, split or not', () => {
      const content = '# Heading\n\nA paragraph with **bold**.\n\n- one\n- two';
      const {container} = render(<MarkdownMessage content={content} />);
      const {container: singleBlock} = render(
        <MarkdownMessage content="A paragraph." />,
      );

      expect(normalize(container.firstElementChild!.innerHTML)).toBe(
        '<p class="first:mt-0 last:mb-0 mt-3 mb-1.5 font-medium">Heading</p>' +
          '<p class="first:mt-0 last:mb-0 my-2 leading-[1.55]">A paragraph with <strong class="font-medium">bold</strong>.</p>' +
          '<ul class="first:mt-0 last:mb-0 my-2 list-disc space-y-1 pl-5"><li class="leading-[1.5]">one</li><li class="leading-[1.5]">two</li></ul>',
      );
      expect(normalize(singleBlock.firstElementChild!.innerHTML)).toBe(
        '<p class="first:mt-0 last:mb-0 my-2 leading-[1.55]">A paragraph.</p>',
      );
    });

    it('still escapes raw HTML that spans multiple top-level blocks', () => {
      const {container} = render(
        <MarkdownMessage
          content={'<div>one</div>\n\n<div>two</div>\n\nplain text'}
        />,
      );

      expect(container.firstElementChild!.querySelector('div')).toBeNull();
      expect(screen.getByText(/plain text/)).toBeInTheDocument();
    });
  });
});

describe('markdown message code', () => {
  afterEach(() => {
    Reflect.deleteProperty(navigator, 'clipboard');
  });

  function stubClipboard(writeText: ReturnType<typeof vi.fn>) {
    Object.defineProperty(navigator, 'clipboard', {
      value: {writeText},
      configurable: true,
    });
  }

  describe('MarkdownMessage fenced code blocks', () => {
    it('copies the block source to the clipboard and reports success', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      stubClipboard(writeText);
      render(<MarkdownMessage content={'```js\nconst x = 1;\n```'} />);

      fireEvent.click(screen.getByRole('button', {name: /copy code/i}));

      await waitFor(() => expect(writeText).toHaveBeenCalled());
      expect(writeText.mock.calls[0][0]).toContain('const x = 1;');
      await waitFor(() =>
        expect(
          screen.getByRole('button', {name: 'Copied'}),
        ).toBeInTheDocument(),
      );
    });

    it('highlights registered languages and leaves others as plain code', () => {
      const {container} = render(
        <MarkdownMessage
          content={
            '```python\nimport os\n```\n\n```haskell\nmain = pure ()\n```'
          }
        />,
      );

      const [py, hs] = Array.from(container.querySelectorAll('code'));
      expect(py.querySelector('.hljs-keyword')).not.toBeNull();
      expect(hs.querySelector('[class^="hljs-"]')).toBeNull();
      expect(hs.textContent).toBe('main = pure ()\n');
    });

    it('does not crash or claim success when the clipboard rejects', async () => {
      const writeText = vi.fn().mockRejectedValue(new Error('denied'));
      stubClipboard(writeText);
      const {container} = render(
        <MarkdownMessage content={'```js\nconst x = 1;\n```'} />,
      );

      fireEvent.click(screen.getByRole('button', {name: /copy code/i}));

      await waitFor(() => expect(writeText).toHaveBeenCalled());
      // Highlighting splits code into spans, requiring textContent rather than
      // a single text node.
      expect(container.querySelector('code')?.textContent).toContain(
        'const x = 1;',
      );
      await waitFor(() =>
        expect(
          screen.getByRole('button', {name: /copy code/i}),
        ).toBeInTheDocument(),
      );
      expect(screen.queryByRole('button', {name: 'Copied'})).toBeNull();
    });
  });
});

describe('splitMarkdownIntoBlocks', () => {
  it('splits a multi-block message into one exact slice per block', () => {
    const content = 'First paragraph.\n\n- one\n- two\n\n# Heading';
    expect(splitMarkdownIntoBlocks(content)).toEqual([
      'First paragraph.',
      '- one\n- two',
      '# Heading',
    ]);
  });

  it('renders whole when a reference definition sits in a later block', () => {
    const content =
      'See [docs][d].\n\nSome other paragraph.\n\n[d]: https://example.com';
    expect(splitMarkdownIntoBlocks(content)).toEqual([content]);
  });

  it('renders whole when a top-level block is raw HTML', () => {
    const content = '<div>one</div>\n\n<div>two</div>';
    expect(splitMarkdownIntoBlocks(content)).toEqual([content]);
  });
});
