import {expect, it} from 'vitest';
import {displayTitle} from './titles';
import {ellipsize, titleCase} from './text';

it('prefers a trimmed stored title', () => {
  expect(displayTitle('  Named  ', 'goal')).toBe('Named');
});

it('falls back to the shortened goal for a blank title', () => {
  expect(displayTitle('   ', 'Find a drug', goal => `short:${goal}`)).toBe(
    'short:Find a Drug',
  );
});

it('reads Untitled session when neither title nor goal has text', () => {
  expect(displayTitle(null, '')).toBe('Untitled session');
  expect(displayTitle('  ', '   ')).toBe('Untitled session');
  expect(displayTitle(undefined, null, goal => goal)).toBe('Untitled session');
});

it('shows titles in title case without touching scientific spellings', () => {
  expect(displayTitle('hello', '')).toBe('Hello');
  expect(titleCase('role of mRNA decay in the FDA-approved p53 pathway')).toBe(
    'Role of mRNA Decay in the FDA-approved p53 Pathway',
  );
  expect(titleCase('β-catenin signalling in glioma')).toBe(
    'β-catenin Signalling in Glioma',
  );
  expect(titleCase('what to look for')).toBe('What to Look For');
});

it('drops punctuation before an ellipsis', () => {
  expect(ellipsize('Identify an FDA-approved,')).toBe(
    'Identify an FDA-approved…',
  );
  expect(ellipsize('Effects of drugs:')).toBe('Effects of drugs…');
  expect(ellipsize('Plain words')).toBe('Plain words…');
});
