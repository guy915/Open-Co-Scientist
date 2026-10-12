import {
  isValidElement,
  memo,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {rehypeHighlightKnownLanguages} from './highlight_code';
import {CopyButton} from './copy_button';
import {fromMarkdown} from 'mdast-util-from-markdown';
import {gfm} from 'micromark-extension-gfm';
import {gfmFromMarkdown} from 'mdast-util-gfm';
import {ExternalLink} from './external_link';

const REMARK_PLUGINS = [remarkGfm];

// Highlight only the parsed tree, never raw HTML.
const REHYPE_PLUGINS = [rehypeHighlightKnownLanguages];

// Never enable rehype-raw for untrusted model output. Scientist text stays
// plain because its collapse measurement depends on that span.

// Chat blocks need tighter spacing than reports, with no dead space at bubble
// edges.
const BLOCK = 'first:mt-0 last:mb-0';

type Props<Tag extends keyof React.JSX.IntrinsicElements> =
  ComponentPropsWithoutRef<Tag>;

// Citation links must not navigate away from live chat.
function MarkdownLink({href, children}: Props<'a'>) {
  return (
    <ExternalLink
      href={href}
      fallback={children}
      className="text-cosci-blue underline underline-offset-2"
    >
      {children}
    </ExternalLink>
  );
}

// Wide tables must scroll locally rather than widen the chat.
function MarkdownTable({children}: Props<'table'>) {
  return (
    <div className={`${BLOCK} my-3 overflow-x-auto`}>
      <table className="w-full border-collapse text-left text-sm">
        {children}
      </table>
    </div>
  );
}

// Local typography prevents unrelated report styling from changing chat prose.
const COMPONENTS = {
  a: MarkdownLink,
  // Model-selected URLs must never make a request from the reader's browser.
  img: ({alt, src}: Props<'img'>) => (
    <span>
      {alt || 'Image'}
      {src ? ` (${src})` : ''}
    </span>
  ),
  table: MarkdownTable,
  p: ({children}: Props<'p'>) => (
    <p className={`${BLOCK} my-2 leading-[1.55]`}>{children}</p>
  ),
  ul: ({children}: Props<'ul'>) => (
    <ul className={`${BLOCK} my-2 list-disc space-y-1 pl-5`}>{children}</ul>
  ),
  ol: ({children}: Props<'ol'>) => (
    <ol className={`${BLOCK} my-2 list-decimal space-y-1 pl-5`}>{children}</ol>
  ),
  li: ({children}: Props<'li'>) => (
    <li className="leading-[1.5]">{children}</li>
  ),
  // Emphasis inherits its surface color so bold thinking text cannot become a
  // different voice.
  strong: ({children}: Props<'strong'>) => (
    <strong className="font-medium">{children}</strong>
  ),
  code: ({children}: Props<'code'>) => (
    <code className="rounded-md bg-cosci-hover px-1 py-0.5 font-mono text-[0.9em]">
      {children}
    </code>
  ),
  pre: MarkdownPre,
  // All heading levels share chat-scale typography rather than document-sized
  // headings.
  h1: ({children}: Props<'h1'>) => (
    <MarkdownHeading>{children}</MarkdownHeading>
  ),
  h2: ({children}: Props<'h2'>) => (
    <MarkdownHeading>{children}</MarkdownHeading>
  ),
  h3: ({children}: Props<'h3'>) => (
    <MarkdownHeading>{children}</MarkdownHeading>
  ),
  h4: ({children}: Props<'h4'>) => (
    <MarkdownHeading>{children}</MarkdownHeading>
  ),
  th: ({children}: Props<'th'>) => (
    <th className="border-b border-cosci-border px-2 py-1.5 font-medium">
      {children}
    </th>
  ),
  td: ({children}: Props<'td'>) => (
    <td className="border-b border-cosci-border px-2 py-1.5 align-top">
      {children}
    </td>
  ),
  blockquote: ({children}: Props<'blockquote'>) => (
    <blockquote
      className={`${BLOCK} my-2 border-l-2 border-cosci-border pl-3 text-cosci-muted`}
    >
      {children}
    </blockquote>
  ),
  hr: () => <hr className={`${BLOCK} my-3 border-cosci-border`} />,
};

function MarkdownHeading({children}: {children: ReactNode}) {
  return <p className={`${BLOCK} mt-3 mb-1.5 font-medium`}>{children}</p>;
}

function fenceLanguage(className: string | undefined): string | null {
  return /language-([\w-]+)/.exec(className ?? '')?.[1] ?? null;
}

// Highlighting preserves string leaves; concatenate them to copy exact source
// rather than rendered DOM text.
function highlightedText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(highlightedText).join('');
  }
  if (isValidElement<{children?: ReactNode}>(node)) {
    return highlightedText(node.props.children);
  }
  return '';
}

function CodeBlockHeader({
  language,
  text,
}: {
  language: string | null;
  text: string;
}) {
  return (
    <div className="flex items-center gap-2 border-b border-cosci-border px-3 py-1.5 text-xs text-cosci-muted">
      {language && <span className="font-mono">{language}</span>}
      <CopyButton
        text={text}
        label="Copy code"
        size="xs"
        tooltipPlacement="left"
        layoutClassName="ml-auto"
      />
    </div>
  );
}

// react-markdown v10 removed inline-code metadata; the pre wrapper reads raw
// code-child props. Keep copy chrome outside horizontal scrolling.
function MarkdownPre({children}: Props<'pre'>) {
  const codeElement = isValidElement<{
    className?: string;
    children?: ReactNode;
  }>(children)
    ? children
    : null;
  const {className, children: codeChildren} = codeElement?.props ?? {};
  const language = fenceLanguage(className);

  return (
    <div
      className={`${BLOCK} my-3 overflow-hidden rounded-md border border-cosci-border`}
    >
      <CodeBlockHeader
        language={language}
        text={highlightedText(codeChildren)}
      />
      <pre className="m-0 overflow-x-auto bg-cosci-hover p-3 text-sm">
        <code className={className}>{codeChildren}</code>
      </pre>
    </div>
  );
}

// Memoized exact slices prevent reparsing completed blocks on every fragment;
// position keys avoid collisions between identical blocks.
const MarkdownBlock = memo(({raw}: {raw: string}) => (
  <Markdown
    remarkPlugins={REMARK_PLUGINS}
    rehypePlugins={REHYPE_PLUGINS}
    components={COMPONENTS}
  >
    {raw}
  </Markdown>
));

// Per-block rendering preserves direct DOM siblings, so first/last spacing
// selectors retain whole-message semantics.
export function MarkdownMessageRenderer({
  content,
  className = '',
}: {
  content: string;
  className?: string;
}) {
  return (
    // Normal whitespace prevents emitted block-separator newlines adding blank
    // lines; paragraphs inside list items need zero margins to avoid doubled loose-list
    // spacing.
    <div
      className={`min-w-0 break-words whitespace-normal [&_li_p]:my-0 ${className}`}
    >
      {splitMarkdownIntoBlocks(content).map((raw, index) => (
        // Stable positions avoid duplicate-text keys while streaming grows only
        // the final block.
        <MarkdownBlock key={index} raw={raw} />
      ))}
    </div>
  );
}

// Block splitting derives from LibreChat splitMarkdown.ts (MIT); only GFM is
// supported here.
// Parsed mdast source nodes have resolved offsets; optional position also
// admits hand-built nodes.
interface MdastNode {
  type: string;
  children?: MdastNode[];
  position?: {start: {offset: number}; end: {offset: number}};
}

// An mdast Root always carries children, including when empty.
const parseToMdast = (content: string): {children: MdastNode[]} =>
  fromMarkdown(content, {
    extensions: [gfm()],
    mdastExtensions: [gfmFromMarkdown()],
  }) as {children: MdastNode[]};

// Reference/footnote definitions are document-scoped; splitting them from
// references changes rendering, so keep that message whole.
const containsDefinition = (node: MdastNode): boolean => {
  if (node.type === 'definition' || node.type === 'footnoteDefinition') {
    return true;
  }
  return (node.children ?? []).some(containsDefinition);
};

// Only top-level HTML blocks lose separator whitespace on splitting;
// nested/inline HTML stays escaped without rehype-raw.
const hasTopLevelHtml = (children: MdastNode[]): boolean =>
  children.some(node => node.type === 'html');

const requiresWholeMessage = (children: MdastNode[]): boolean =>
  hasTopLevelHtml(children) || children.some(containsDefinition);

function nodeOffsets(node: MdastNode): [number, number] | undefined {
  const position = node.position;
  if (position === undefined) {
    return undefined;
  }
  return [position.start.offset, position.end.offset];
}

// Untrusted or absent source offsets must use the whole-message fallback.
function sliceBlocks(
  content: string,
  children: MdastNode[],
): string[] | undefined {
  const slices: string[] = [];
  for (const node of children) {
    const offsets = nodeOffsets(node);
    if (offsets === undefined) {
      return undefined;
    }
    slices.push(content.slice(offsets[0], offsets[1]));
  }
  return slices;
}

// Exact completed source slices remain stable across streaming; block margins
// replace inter-block whitespace.
export function splitMarkdownIntoBlocks(content: string): string[] {
  if (!content) {
    return [];
  }

  const children = parseToMdast(content).children;
  if (children.length === 0 || requiresWholeMessage(children)) {
    return [content];
  }

  const slices = sliceBlocks(content, children);
  if (slices === undefined) {
    return [content];
  }
  return slices;
}
