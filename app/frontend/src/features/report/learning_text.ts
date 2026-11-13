// Reader shaping for Knowledge Base topics. Saved reports keep their text as
// written; these helpers only decide how it is shown.

// A bracket group holding only generation keys, e.g. "[C1]" or "[C6, C7]".
// The Learning view lists references on their own, so the keys add noise.
const CITATION_KEY_GROUP = /\s*\[C\d+(?:\s*,\s*C\d+)*\]/g;

export function stripCitationKeys(text: string): string {
  return text.replace(CITATION_KEY_GROUP, '').trim();
}

function sentences(text: string): string[] {
  return Array.from(
    new Intl.Segmenter('en', {granularity: 'sentence'}).segment(text),
    item => item.segment.trim(),
  ).filter(Boolean);
}

// The Summary is one sentence. A longer saved summary keeps its first
// sentence there, and the whole text moves to the top of the details so
// nothing is lost.
export function splitSummary(
  summary: string,
  detail: string,
): {summary: string; detail: string} {
  const clean = stripCitationKeys(summary);
  const parts = sentences(clean);
  const cleanDetail = stripCitationKeys(detail);
  if (parts.length <= 1) return {summary: clean, detail: cleanDetail};
  const lead = cleanDetail.includes(clean) ? '' : clean;
  return {
    summary: parts[0],
    detail: [lead, cleanDetail].filter(Boolean).join('\n\n'),
  };
}

export type DetailBlock =
  | {kind: 'paragraph'; text: string}
  | {kind: 'steps'; items: string[]}
  | {kind: 'criterion'; label: string; text: string};

// Older reports joined a pilot plan into one line: "Step 1: … Step 2: …
// Go: … No-Go: …". Break before each marker so the plan reads as a list.
const STEP_MARKER = String.raw`Step \d+(?: \([^)]*\))?:`;
const MARKER_BREAK = new RegExp(
  String.raw`\s+(?=${STEP_MARKER}|(?<!No-)Go:\s|No-Go:\s)`,
  'g',
);
const STEP_LINE = new RegExp(`^${STEP_MARKER}`);
const CRITERION_LINE = /^(No-Go|Go):\s*/;

function pushSegment(blocks: DetailBlock[], segment: string) {
  const criterion = CRITERION_LINE.exec(segment);
  if (criterion) {
    blocks.push({
      kind: 'criterion',
      label: criterion[1],
      text: segment.slice(criterion[0].length),
    });
    return;
  }
  if (STEP_LINE.test(segment)) {
    const last = blocks[blocks.length - 1];
    if (last?.kind === 'steps') last.items.push(segment);
    else blocks.push({kind: 'steps', items: [segment]});
    return;
  }
  blocks.push({kind: 'paragraph', text: segment});
}

export function detailBlocks(detail: string): DetailBlock[] {
  const blocks: DetailBlock[] = [];
  for (const line of detail.split(/\n+/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    for (const segment of trimmed.split(MARKER_BREAK)) {
      if (segment.trim()) pushSegment(blocks, segment.trim());
    }
  }
  return blocks;
}
