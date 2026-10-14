import {describe, expect, it} from 'vitest';
import {renderInlineHtml} from './sanitize_html';

describe('renderInlineHtml', () => {
  it('preserves whitelisted inline formatting tags', () => {
    expect(renderInlineHtml('Resistance in <i>Mycobacterium</i>')).toBe(
      'Resistance in <i>Mycobacterium</i>',
    );
    expect(renderInlineHtml('H<sub>2</sub>O and E=mc<sup>2</sup>')).toBe(
      'H<sub>2</sub>O and E=mc<sup>2</sup>',
    );
  });

  it('escapes disallowed tags into inert text', () => {
    expect(renderInlineHtml('<script>alert(1)</script>')).toBe(
      '&lt;script&gt;alert(1)&lt;/script&gt;',
    );
    expect(renderInlineHtml('<a href="evil">x</a>')).toBe(
      '&lt;a href="evil"&gt;x&lt;/a&gt;',
    );
  });

  it('strips attributes from whitelisted tags', () => {
    expect(renderInlineHtml('<i onclick="steal()">x</i>')).toBe(
      '&lt;i onclick="steal()"&gt;x</i>',
    );
  });

  it('neutralizes the slash-separated attribute bypass', () => {
    const payload = '<img/src=x/onerror=alert(1)>';
    const result = renderInlineHtml(payload);
    expect(result).not.toMatch(/<[a-z]/i);
    expect(result).toBe('&lt;img/src=x/onerror=alert(1)&gt;');
  });

  it('escapes ampersands so entities cannot be forged', () => {
    expect(renderInlineHtml('Tom & Jerry <b>bold</b>')).toBe(
      'Tom &amp; Jerry <b>bold</b>',
    );
  });
});
