import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import worker from './worker.mjs';

function assets() {
  return {
    async fetch(request) {
      const path = new URL(request.url).pathname;
      const body = path === '/index.html' ? '<main>Co-Scientist</main>' : 'asset';
      const status = ['/index.html', '/assets/app.js', '/robots.txt'].includes(path)
        ? 200
        : 404;
      const headers = path.startsWith('/assets/')
        ? {'Cache-Control': 'public, max-age=31536000, immutable'}
        : {};
      return new Response(request.method === 'HEAD' ? null : body, {status, headers});
    },
  };
}

test('deep links reload the workbench and missing bundles stay 404', async () => {
  const env = {ASSETS: assets()};
  const deepLink = await worker.fetch(
    new Request('https://open-coscientist.com/runs/private-run?tab=report'), env,
  );
  assert.equal(deepLink.status, 200);
  assert.equal(await deepLink.text(), '<main>Co-Scientist</main>');

  const missing = await worker.fetch(
    new Request('https://open-coscientist.com/assets/missing.js'), env,
  );
  assert.equal(missing.status, 404);

  for (const path of ['/assets/app.js', '/robots.txt']) {
    const response = await worker.fetch(new Request(`https://open-coscientist.com${path}`), env);
    assert.equal(await response.text(), 'asset');
  }

  const head = await worker.fetch(
    new Request('https://open-coscientist.com/chats/private-chat', {method: 'HEAD'}), env,
  );
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
});

test('the worker is served only on its custom domain', () => {
  const config = JSON.parse(readFileSync(new URL('../../wrangler.jsonc', import.meta.url), 'utf8'));
  assert.equal(config.workers_dev, false);
  assert.equal(config.preview_urls, false);
});

test('a missing bundle is never cached while present bundles stay immutable', async () => {
  const env = {ASSETS: assets()};

  const missing = await worker.fetch(
    new Request('https://open-coscientist.com/assets/missing.js'), env,
  );
  const present = await worker.fetch(new Request('https://open-coscientist.com/assets/app.js'), env);

  assert.equal(missing.status, 404);
  assert.equal(missing.headers.get('Cache-Control'), 'no-store');
  assert.equal(present.headers.get('Cache-Control'), 'public, max-age=31536000, immutable');
});
