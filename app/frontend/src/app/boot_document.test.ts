import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {afterEach, describe, expect, it, vi} from 'vitest';

const html = readFileSync(resolve(__dirname, '../../index.html'), 'utf8');
const headers = readFileSync(
  resolve(__dirname, '../../public/_headers'),
  'utf8',
);
// Every classic inline script runs under the CSP; JSON-LD data blocks do not.
const inlineScripts = Array.from(
  new DOMParser()
    .parseFromString(html, 'text/html')
    .querySelectorAll('script:not([src]):not([type])'),
  script => script.textContent ?? '',
);

function runBootScript(
  path: string,
  stored: string | null,
  systemDark: boolean,
) {
  window.history.pushState({}, '', path);
  if (stored === null) localStorage.removeItem('cosci-theme');
  else localStorage.setItem('cosci-theme', stored);
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: systemDark && query === '(prefers-color-scheme: dark)',
  }));
  new Function(inlineScripts[0])();
  const {theme, boot} = document.documentElement.dataset;
  return {theme, boot};
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
  window.history.pushState({}, '', '/');
});

it('allows every inline script in the production CSP', () => {
  const scriptSources = headers
    .split('Content-Security-Policy: ')[1]
    .split(';')
    .find(directive => directive.trim().startsWith('script-src'));
  expect(inlineScripts).toHaveLength(1);
  for (const script of inlineScripts) {
    const hash = createHash('sha256').update(script).digest('base64');
    expect(scriptSources).toContain(`'sha256-${hash}'`);
  }
});

describe('boot script', () => {
  it('paints the stored theme over the system preference', () => {
    expect(runBootScript('/', 'dark', false).theme).toBe('dark');
    expect(runBootScript('/', 'light', true).theme).toBe('light');
    expect(runBootScript('/', 'system', true).theme).toBe('dark');
    expect(runBootScript('/', null, false).theme).toBe('light');
  });

  it('follows the system theme when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(runBootScript('/', null, true).theme).toBe('dark');
  });

  it.each([
    ['/', 'home'],
    ['/runs', 'home'],
    ['/runs/new', 'home'],
    ['/chats/chat-1', 'chat'],
    ['/examples/example-1', 'chat'],
    ['/runs/run-1/details', 'report'],
    ['/operations', 'page'],
  ])('shapes the skeleton for %s as %s', (path, boot) => {
    expect(runBootScript(path, null, false).boot).toBe(boot);
  });
});
