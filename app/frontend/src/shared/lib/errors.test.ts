import {expect, it} from 'vitest';
import {HttpError} from '@/shared/api/runs';
import {errorMessage, httpStatus} from './errors';

it('reads an Error message or falls back', () => {
  expect(errorMessage(new Error('boom'))).toBe('boom');
  expect(errorMessage('plain')).toBe('plain');
  expect(errorMessage(42, 'Could not save.')).toBe('Could not save.');
});

it('reads the status an API error carries', () => {
  expect(httpStatus(new HttpError('nope', 422))).toBe(422);
  expect(httpStatus(new Error('no status'))).toBeUndefined();
});

it.each([
  'Failed to fetch',
  'NetworkError when attempting to fetch resource.',
  'Load failed',
])('gives a connection instruction for browser fetch failure: %s', message => {
  expect(errorMessage(new TypeError(message))).toBe(
    'Cannot connect to the server. Check your connection and try again.',
  );
});

it('preserves API refusal details and unrelated programming errors', () => {
  expect(errorMessage(new HttpError('Daily run limit reached.', 429))).toBe(
    'Daily run limit reached.',
  );
  expect(
    errorMessage(new TypeError('Cannot read properties of undefined')),
  ).toBe('Cannot read properties of undefined');
  expect(errorMessage(new Error('Failed to fetch'))).toBe('Failed to fetch');
});
