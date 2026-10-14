import {expect, it} from 'vitest';
import {planLeadIn} from './chat_timeline_run_spec_card';

// Real completing interview turns restate plan fields in structured prose.

const WITH_LEAD_IN = [
  '## Scope finalized',
  '',
  'I have incorporated the epigenetic preference and the testability',
  'requirement.',
  '',
  '### Research Challenge',
  'How can epigenetic mechanisms deactivate hepatic stellate cells?',
  '',
  '### Focus Area',
  '- Epigenetic mechanisms of hepatic stellate cell deactivation',
  '',
  '### Title',
  'Epigenetic HSC deactivation for reversal of MASH fibrosis',
  '',
  'This scope is ready to start, or it can be refined further before the run.',
].join('\n');

it('drops the sections that restate the plan document own fields', () => {
  const lead = planLeadIn(WITH_LEAD_IN);

  expect(lead).not.toContain('Research Challenge');
  expect(lead).not.toContain('hepatic stellate cells?');
  expect(lead).not.toContain('Focus Area');
  expect(lead).not.toContain('Epigenetic HSC deactivation for reversal');
});

it('keeps the prose the model wrote around the summary', () => {
  const lead = planLeadIn(WITH_LEAD_IN);

  expect(lead).toContain('## Scope finalized');
  expect(lead).toContain('I have incorporated the epigenetic preference');
  expect(lead).toContain('This scope is ready to start');
});
