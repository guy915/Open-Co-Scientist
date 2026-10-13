// External titles/abstracts contain inline formatting but are untrusted. Escape
// first, then restore only exact attribute-less allowlisted tags.

const INLINE_TAGS = ['i', 'b', 'em', 'strong', 'sub', 'sup', 'u'] as const;

const ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>]/g, char => ESCAPE_MAP[char]);
}

export function renderInlineHtml(raw: string): string {
  let out = escapeHtml(raw);
  // Restore exact bare tags only; attributes, handlers and URLs must remain
  // escaped.
  for (const tag of INLINE_TAGS) {
    out = out
      .replaceAll(`&lt;${tag}&gt;`, `<${tag}>`)
      .replaceAll(`&lt;/${tag}&gt;`, `</${tag}>`);
  }
  return out;
}
