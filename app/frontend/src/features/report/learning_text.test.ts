import {describe, expect, it} from 'vitest';
import {detailBlocks, splitSummary, stripCitationKeys} from './learning_text';

describe('learning text', () => {
  it('drops generation citation keys', () => {
    expect(stripCitationKeys('Organoids capture heterogeneity [C6, C7].')).toBe(
      'Organoids capture heterogeneity.',
    );
  });

  it('keeps one sentence as the summary and moves the rest to the details', () => {
    const {summary, detail} = splitSummary(
      'Exposure is the key filter [C1]. Frameworks were validated [C4].',
      'Step 1: Search.',
    );
    expect(summary).toBe('Exposure is the key filter.');
    expect(detail).toBe(
      'Exposure is the key filter. Frameworks were validated.\n\nStep 1: Search.',
    );
  });

  it('leaves a one-sentence summary and its details alone', () => {
    expect(splitSummary('One finding.', 'More detail.')).toEqual({
      summary: 'One finding.',
      detail: 'More detail.',
    });
  });

  it('breaks a run-on pilot plan into steps and Go/No-Go criteria', () => {
    const blocks = detailBlocks(
      'Step 1: Verify zero hits. Step 2: Assemble the panel. ' +
        'Step 3 (Go/No-Go initial experiment): Evaluate SI. ' +
        'Go: SI ≥10 in 3 of 5 lines. No-Go: SI <10.',
    );
    expect(blocks).toEqual([
      {
        kind: 'steps',
        items: [
          'Step 1: Verify zero hits.',
          'Step 2: Assemble the panel.',
          'Step 3 (Go/No-Go initial experiment): Evaluate SI.',
        ],
      },
      {kind: 'criterion', label: 'Go', text: 'SI ≥10 in 3 of 5 lines.'},
      {kind: 'criterion', label: 'No-Go', text: 'SI <10.'},
    ]);
  });

  it('keeps prose that only mentions a step as one paragraph', () => {
    expect(detailBlocks('As in Step 2 of the plan, go on.')).toEqual([
      {kind: 'paragraph', text: 'As in Step 2 of the plan, go on.'},
    ]);
  });

  it('splits paragraphs on line breaks', () => {
    expect(detailBlocks('Grounding.\n\nStep 1: Do it.\nGo: Yes.')).toEqual([
      {kind: 'paragraph', text: 'Grounding.'},
      {kind: 'steps', items: ['Step 1: Do it.']},
      {kind: 'criterion', label: 'Go', text: 'Yes.'},
    ]);
  });
});
