import {afterEach, expect, it, vi} from 'vitest';
import {copyText} from './clipboard';

afterEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard');
  document.body.replaceChildren();
});

it('uses the async Clipboard API when available', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    value: {writeText},
    configurable: true,
  });
  const execCommand = vi.fn();
  Object.defineProperty(document, 'execCommand', {
    value: execCommand,
    configurable: true,
  });

  await expect(copyText('hello world')).resolves.toBe(true);

  expect(writeText).toHaveBeenCalledWith('hello world');
  expect(execCommand).not.toHaveBeenCalled();
});

it('falls back to execCommand when the Clipboard API rejects', async () => {
  const writeText = vi.fn().mockRejectedValue(new Error('denied'));
  Object.defineProperty(navigator, 'clipboard', {
    value: {writeText},
    configurable: true,
  });
  const execCommand = vi.fn().mockReturnValue(true);
  Object.defineProperty(document, 'execCommand', {
    value: execCommand,
    configurable: true,
  });

  await expect(copyText('retry text')).resolves.toBe(true);

  expect(writeText).toHaveBeenCalledWith('retry text');
  expect(execCommand).toHaveBeenCalledWith('copy');
});

it('reports failure without throwing when no copy path works', async () => {
  Reflect.deleteProperty(navigator, 'clipboard');
  Object.defineProperty(document, 'execCommand', {
    value: () => {
      throw new Error('not supported');
    },
    configurable: true,
  });

  await expect(copyText('no-op')).resolves.toBe(false);
});
