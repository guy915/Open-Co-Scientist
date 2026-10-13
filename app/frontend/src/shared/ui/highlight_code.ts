import bash from 'highlight.js/lib/languages/bash';
import diff from 'highlight.js/lib/languages/diff';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import latex from 'highlight.js/lib/languages/latex';
import markdown from 'highlight.js/lib/languages/markdown';
import plaintext from 'highlight.js/lib/languages/plaintext';
import python from 'highlight.js/lib/languages/python';
import r from 'highlight.js/lib/languages/r';
import shell from 'highlight.js/lib/languages/shell';
import sql from 'highlight.js/lib/languages/sql';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';
import {createLowlight} from 'lowlight';

// rehype-highlight statically imports lowlight's ~37-language default set, so
// no options can drop it from the bundle; this plugin registers only the
// grammars a research chat plausibly renders. Other fences stay plain code.
const lowlight = createLowlight({
  bash,
  diff,
  javascript,
  json,
  latex,
  markdown,
  plaintext,
  python,
  r,
  shell,
  sql,
  typescript,
  xml,
  yaml,
});

interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: {className?: unknown};
  children?: HastNode[];
}

function fenceLanguage(code: HastNode): string | undefined {
  const classes = code.properties?.className;
  if (!Array.isArray(classes)) {
    return undefined;
  }
  const match = classes.find(
    (name): name is string =>
      typeof name === 'string' && name.startsWith('language-'),
  );
  return match?.slice('language-'.length);
}

function highlightFences(node: HastNode): void {
  for (const child of node.children ?? []) {
    const isFence = node.tagName === 'pre' && child.tagName === 'code';
    const language = isFence ? fenceLanguage(child) : undefined;
    const source = child.children?.map(part => part.value ?? '').join('');
    if (language && lowlight.registered(language) && source !== undefined) {
      child.properties = {
        ...child.properties,
        className: ['hljs', ...(child.properties?.className as string[])],
      };
      child.children = lowlight.highlight(language, source).children;
    } else {
      highlightFences(child);
    }
  }
}

export function rehypeHighlightKnownLanguages() {
  return (tree: HastNode): void => highlightFences(tree);
}
