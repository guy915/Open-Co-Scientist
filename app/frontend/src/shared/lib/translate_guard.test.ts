import {afterEach, beforeAll, expect, it} from 'vitest';
import {installTranslateGuard} from './translate_guard';

beforeAll(() => installTranslateGuard());
afterEach(() => {
  document.documentElement.className = '';
});

function detached() {
  const parent = document.createElement('div');
  const stranger = document.createElement('span');
  document.createElement('p').append(stranger);
  return {parent, stranger};
}

it('ignores a stale removal on a translated page', () => {
  document.documentElement.className = 'translated-ltr';
  const {parent, stranger} = detached();
  expect(parent.removeChild(stranger)).toBe(stranger);
  expect(stranger.parentNode).not.toBe(parent);
});

it('ignores a stale insertion point on a translated page', () => {
  document.documentElement.className = 'translated-rtl';
  const {parent, stranger} = detached();
  const node = document.createElement('b');
  expect(parent.insertBefore(node, stranger)).toBe(node);
});

it('keeps native errors on an untranslated page', () => {
  const {parent, stranger} = detached();
  expect(() => parent.removeChild(stranger)).toThrow();
  expect(() =>
    parent.insertBefore(document.createElement('b'), stranger),
  ).toThrow();
});

it('still removes and inserts real children', () => {
  document.documentElement.className = 'translated-ltr';
  const parent = document.createElement('div');
  const child = document.createElement('span');
  parent.append(child);
  const before = document.createElement('b');
  parent.insertBefore(before, child);
  expect(parent.firstChild).toBe(before);
  parent.removeChild(child);
  expect(parent.contains(child)).toBe(false);
});
