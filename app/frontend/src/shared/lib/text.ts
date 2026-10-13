import {renderInlineHtml} from './sanitize_html';

export function conciseTitle(goal: string, maxChars = 52): string {
  const trimmed = (goal ?? '').trim();
  if (!trimmed) return 'Untitled session';
  const firstClause = firstSentenceClause(trimmed);
  if (firstClause.length <= maxChars) return firstClause;
  return truncateOnWordBoundary(firstClause, maxChars);
}

// Sentence boundaries require whitespace/end so scientific abbreviations such
// as M.tuberculosis remain intact.
export function firstSentenceClause(text: string): string {
  const source = (text ?? '').trim();
  const sentenceEnd = source.search(/[.?!;](\s|$)/);
  return (sentenceEnd >= 0 ? source.slice(0, sentenceEnd) : source).trim();
}

// Only lowercase ASCII first words may change: scientific forms such as mRNA
// and Greek prefixes must retain their case.
export function capitalizeTerm(term: string): string {
  const text = term.trim();
  const firstWord = text.split(/\s/, 1)[0] ?? '';
  if (!/^[a-z][a-z-]*$/.test(firstWord)) return text;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// json_object output does not enforce field types; flatten malformed/serialized
// values rather than expose raw JSON.
export function readableText(value: unknown): string {
  if (typeof value === 'string') return readableFromString(value);
  if (Array.isArray(value)) return joinReadable(value, ' ');
  if (isRecord(value)) return joinReadable(Object.values(value), ' - ');
  return primitiveText(value);
}

export function readableTextList(value: unknown): string[] {
  if (typeof value === 'string') return listFromString(value);
  if (Array.isArray(value)) return mapReadable(value);
  if (isRecord(value)) return mapReadable([value]);
  return [];
}

function readableFromString(value: string): string {
  const trimmed = value.trim();
  if (!isJsonLike(trimmed)) return value;
  try {
    return readableText(JSON.parse(trimmed));
  } catch {
    return value;
  }
}

function listFromString(value: string): string[] {
  const trimmed = value.trim();
  if (!trimmed) return [];
  if (!isJsonLike(trimmed)) return [trimmed];
  try {
    return readableTextList(JSON.parse(trimmed));
  } catch {
    return [trimmed];
  }
}

function isJsonLike(text: string): boolean {
  return (
    (text.startsWith('{') && text.endsWith('}')) ||
    (text.startsWith('[') && text.endsWith(']'))
  );
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function joinReadable(values: unknown[], separator: string): string {
  return values.map(readableText).filter(Boolean).join(separator);
}

function mapReadable(values: unknown[]): string[] {
  return values.map(readableText).filter(Boolean);
}

function primitiveText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

// Cut text never ends on punctuation before its ellipsis: "FDA-approved…",
// not "FDA-approved,…".
export function ellipsize(text: string): string {
  return `${text.replace(/[\s.,;:!?/\-–—]+$/u, '')}…`;
}

function truncateOnWordBoundary(text: string, maxChars: number): string {
  const cut = text.slice(0, maxChars);
  const lastSpace = cut.lastIndexOf(' ');
  const onBoundary = lastSpace > 0 ? cut.slice(0, lastSpace) : cut;
  const base = onBoundary.replace(/[\s,;:]+$/, '');
  return ellipsize(base.replace(/\s+\S{1,3}$/, '') || base);
}

const MINOR_WORDS = new Set([
  'a',
  'an',
  'and',
  'as',
  'at',
  'but',
  'by',
  'for',
  'from',
  'in',
  'into',
  'nor',
  'of',
  'on',
  'or',
  'per',
  'the',
  'to',
  'versus',
  'via',
  'vs',
  'with',
]);

// Only all-lowercase ASCII words change, so mRNA, FDA-approved, p53 and
// β-catenin keep the case their authors gave them.
function titleWord(word: string, edge: boolean): string {
  const match = /^([^\p{L}\p{N}]*)([a-z][a-z'’-]*)([^\p{L}\p{N}]*)$/u.exec(
    word,
  );
  if (!match) return word;
  const [, lead, core, tail] = match;
  if (!edge && MINOR_WORDS.has(core)) return word;
  return `${lead}${core.charAt(0).toUpperCase()}${core.slice(1)}${tail}`;
}

export function titleCase(text: string): string {
  const words = text.split(/(\s+)/);
  const last = words.length - 1;
  return words
    .map((word, index) =>
      /^\s*$/.test(word)
        ? word
        : titleWord(word, index === 0 || index === last),
    )
    .join('');
}

// NLM structured abstracts concatenate labels without reliable separators,
// including SUMMARYThe and title-case labels followed by two spaces.

const SECTION_LABELS = [
  'MATERIALS AND METHODS',
  'MAIN OUTCOME MEASURES',
  'MAIN OUTCOME MEASURE',
  'BACKGROUND',
  'OBJECTIVES',
  'OBJECTIVE',
  'CONCLUSIONS',
  'CONCLUSION',
  'INTRODUCTION',
  'INTERPRETATION',
  'INTERVENTIONS',
  'INTERVENTION',
  'MEASUREMENTS',
  'PARTICIPANTS',
  'LIMITATIONS',
  'IMPORTANCE',
  'DISCUSSION',
  'RATIONALE',
  'FINDINGS',
  'PURPOSE',
  'METHODS',
  'METHOD',
  'RESULTS',
  'SUMMARY',
  'SETTING',
  'CONTEXT',
  'FUNDING',
  'DESIGN',
  'AIMS',
  'AIM',
];

export interface AbstractSection {
  label: string | null;
  // HTML bodies are sanitized for dangerouslySetInnerHTML.
  html: string;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function labelCase(label: string): string {
  return label.charAt(0).toUpperCase() + label.slice(1).toLowerCase();
}

// Longest labels must win alternation before shorter prefix matches.
const sortedLabels = [...SECTION_LABELS].sort((a, b) => b.length - a.length);
const upperForms = sortedLabels.map(escapeRegExp).join('|');
const titleForms = sortedLabels.map(l => escapeRegExp(labelCase(l))).join('|');

// Two spaces avoid treating ordinary Results were prose as a header; capital
// lookahead must stay case-sensitive.
const LABEL_RE = new RegExp(
  '(?:^|(?<=[.)\\s]))' +
    `(?:(${upperForms})(?::\\s*|\\s{2,}|(?=[A-Z][a-z]))` +
    `|(${titleForms})(?::\\s*|\\s{2,}))`,
  'g',
);

function findLabelMarks(
  text: string,
): {start: number; end: number; label: string}[] {
  const marks: {start: number; end: number; label: string}[] = [];
  LABEL_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = LABEL_RE.exec(text)) !== null) {
    marks.push({
      start: match.index,
      end: LABEL_RE.lastIndex,
      label: match[1] ?? match[2],
    });
    // Advance zero-length matches to prevent an infinite scan.
    if (LABEL_RE.lastIndex === match.index) LABEL_RE.lastIndex++;
  }
  return marks;
}

function withFallback(
  sections: AbstractSection[],
  text: string,
): AbstractSection[] {
  if (sections.length) return sections;
  const trimmed = text.trim();
  return [{label: null, html: trimmed ? renderInlineHtml(trimmed) : ''}];
}

export function splitAbstractSections(raw: string): AbstractSection[] {
  const text = raw ?? '';
  const marks = findLabelMarks(text);

  const sections: AbstractSection[] = [];
  const pushSection = (label: string | null, body: string) => {
    const trimmed = body.trim();
    if (trimmed) sections.push({label, html: renderInlineHtml(trimmed)});
  };

  pushSection(null, text.slice(0, marks[0]?.start ?? text.length));
  marks.forEach((mark, index) => {
    const bodyEnd =
      index + 1 < marks.length ? marks[index + 1].start : text.length;
    pushSection(labelCase(mark.label), text.slice(mark.end, bodyEnd));
  });

  return withFallback(sections, text);
}
