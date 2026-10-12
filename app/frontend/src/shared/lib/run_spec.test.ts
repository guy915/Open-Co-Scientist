import type {Interview} from '@/shared/api/runs';
import {setStoredApiKey} from './client_id';
import {afterEach, describe, expect, it} from 'vitest';
import {availableTierOptions, interviewToRunSpec} from './run_spec';

afterEach(() => setStoredApiKey(''));

describe('interviewToRunSpec', () => {
  it('maps only the durable Agent derivation into run configuration', () => {
    const interview = {
      id: 'interview-1',
      fields: {
        research_challenge: 'Explain treatment resistance.',
        focus_area: ['Tumor metabolism', 'Causal mechanisms'],
        preferences: ['Prioritize human evidence'],
      },
    } as Interview;

    expect(interviewToRunSpec(interview)).toEqual({
      interviewId: 'interview-1',
      goal: 'Explain treatment resistance.',
      requirements: ['Prioritize human evidence'],
      attributes: ['Tumor metabolism', 'Causal mechanisms'],
      criteria: [],
      focus: 'balance',
      tier: 'express',
      notifyOnCompletion: false,
      completionEmail: '',
    });
  });
});

describe('availableTierOptions', () => {
  it('leaves only Express open to free usage', () => {
    const open = availableTierOptions().filter(option => !option.disabled);
    expect(open.map(option => option.id)).toEqual(['express']);
  });

  it('opens every run type with an API key', () => {
    setStoredApiKey('sk-test');
    expect(availableTierOptions().some(option => option.disabled)).toBe(false);
  });
});
