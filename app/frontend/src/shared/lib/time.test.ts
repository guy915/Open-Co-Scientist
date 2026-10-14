import {describe, expect, it} from 'vitest';
import {formatDurationPhrase, relativeTime} from './time';

describe('relativeTime', () => {
  it('is empty without a timestamp', () => {
    expect(relativeTime(undefined, 100)).toBe('');
  });

  it('steps from seconds to minutes to hours', () => {
    expect(relativeTime(1000, 1002)).toBe('just now');
    expect(relativeTime(1000, 1030)).toBe('30s ago');
    expect(relativeTime(1000, 1000 + 600)).toBe('10m ago');
    expect(relativeTime(1000, 1000 + 7200)).toBe('2h ago');
  });

  it('never reports a future stamp as negative', () => {
    expect(relativeTime(2000, 1000)).toBe('just now');
  });
});

describe('formatDurationPhrase', () => {
  it('rounds to minutes and hours', () => {
    expect(formatDurationPhrase(20)).toBe('1 minute');
    expect(formatDurationPhrase(20, {subMinute: true})).toBe('< 1 minute');
    expect(formatDurationPhrase(150)).toBe('3 minutes');
    expect(formatDurationPhrase(7200)).toBe('2 hours');
  });
});
