import type {ComponentProps} from 'react';
import Markdown from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import 'katex/dist/katex.min.css';

type MarkdownComponents = ComponentProps<typeof Markdown>['components'];
type RehypePlugins = ComponentProps<typeof Markdown>['rehypePlugins'];

const REHYPE_KATEX = [rehypeKatex, {throwOnError: false, trust: false}] as [
  typeof rehypeKatex,
  {throwOnError: boolean; trust: boolean},
];

export function MarkdownMathRenderer({
  content,
  components,
  rehypePlugins,
}: {
  content: string;
  components: MarkdownComponents;
  rehypePlugins: RehypePlugins;
}) {
  return (
    <Markdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[REHYPE_KATEX, ...(rehypePlugins ?? [])]}
      components={components}
    >
      {content}
    </Markdown>
  );
}
