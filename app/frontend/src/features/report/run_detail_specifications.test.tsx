import type {RunAttribute} from '@/shared/api/runs';
import {attributeDisplayString} from '@/shared/lib/run_spec';
import {describe, expect, it} from 'vitest';

describe('run detail specifications', () => {
  describe('attributeDisplayString', () => {
    it('passes a legacy free-prose string through unchanged', () => {
      expect(attributeDisplayString('Mechanistically specific')).toBe(
        'Mechanistically specific',
      );
    });

    it('renders a scaled axis with every anchor point', () => {
      const item: RunAttribute = {
        name: 'Mechanistic specificity',
        scale: {
          '1': 'Vague or hand-wavy mechanism',
          '3': 'Plausible mechanism with some unexplained steps',
          '5': 'Precise, causally complete mechanism',
        },
      };
      expect(attributeDisplayString(item)).toBe(
        'Mechanistic specificity: 1-5 scale (1: Vague or hand-wavy ' +
          'mechanism, 3: Plausible mechanism with some unexplained steps, ' +
          '5: Precise, causally complete mechanism)',
      );
    });

    it('renders a scaled axis that omits the midpoint anchor', () => {
      const item: RunAttribute = {
        name: 'Human Relevance',
        scale: {
          '1': 'Based purely on murine or non-liver models',
          '5': 'Strong basis in human liver data',
        },
      };
      expect(attributeDisplayString(item)).toBe(
        'Human Relevance: 1-5 scale (1: Based purely on murine or ' +
          'non-liver models, 5: Strong basis in human liver data)',
      );
    });
  });
});
