import {describe, it, expect} from 'vitest';
import {
  capitalizeTerm,
  conciseTitle,
  readableText,
  readableTextList,
  splitAbstractSections,
} from './text';

describe('text', () => {
  describe('conciseTitle', () => {
    it('takes the first clause of a multi-clause goal', () => {
      expect(
        conciseTitle('Reversing liver fibrosis; detail the mechanism.'),
      ).toBe('Reversing liver fibrosis');
    });

    it('truncates a long goal on a word boundary with an ellipsis', () => {
      const out = conciseTitle(
        'Identify novel mechanisms of selective autophagy in aging neural ' +
          'tissue.',
      );
      expect(out.endsWith('…')).toBe(true);
      expect(out.length).toBeLessThanOrEqual(53);
      expect(out.startsWith('Identify novel mechanisms')).toBe(true);
      expect(out).not.toMatch(/\b(in|of|to|a|the)…$/);
    });
  });

  describe('readableText', () => {
    it('flattens a JSON-string field into plain text', () => {
      const raw =
        '{"significance": "Confirms the core assumption", "gap": "None"}';
      expect(readableText(raw)).toBe('Confirms the core assumption - None');
    });
  });

  describe('readableTextList', () => {
    it('flattens list items that are objects', () => {
      expect(
        readableTextList([
          {experiment: 'Delete relA', rationale: 'test tolerance'},
        ]),
      ).toEqual(['Delete relA - test tolerance']);
    });
  });

  describe('capitalizeTerm', () => {
    it('capitalizes a lowercase noun phrase', () => {
      expect(capitalizeTerm('gut-brain axis')).toBe('Gut-brain axis');
    });
  });
});

describe('format abstract', () => {
  it('returns a single unlabeled section for unstructured text', () => {
    const result = splitAbstractSections('A plain abstract without labels.');
    expect(result).toEqual([
      {label: null, html: 'A plain abstract without labels.'},
    ]);
  });

  it('separates an all-caps label run into the body (real data)', () => {
    const raw =
      'SUMMARYThe global resurgence of drug-resistant tuberculosis (DR-TB) ' +
      'in <i>Mycobacterium tuberculosis</i> (Mtb) is a challenge.';
    const result = splitAbstractSections(raw);
    expect(result).toHaveLength(1);
    expect(result[0].label).toBe('Summary');
    expect(result[0].html).toBe(
      'The global resurgence of drug-resistant tuberculosis (DR-TB) in ' +
        '<i>Mycobacterium tuberculosis</i> (Mtb) is a challenge.',
    );
  });

  it('keeps section bodies XSS-safe', () => {
    const raw = 'Methods  We ran <script>alert(1)</script> analyses.';
    const [section] = splitAbstractSections(raw);
    expect(section.label).toBe('Methods');
    expect(section.html).not.toMatch(/<script/i);
    expect(section.html).toContain('&lt;script&gt;');
  });
});
