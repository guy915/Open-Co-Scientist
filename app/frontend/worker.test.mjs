import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {test} from 'node:test';
import {gunzipSync} from 'node:zlib';
import worker from './worker.mjs';

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const prerenderScript = path.join(frontendRoot, 'scripts/prerender.mjs');

function assets(files = {}) {
  return {
    requests: [],
    async fetch(request) {
      const url = new URL(request.url);
      const {pathname: path} = url;
      this.requests.push({path, method: request.method, search: url.search});
      const body = files[path];
      const status = body === undefined ? 404 : 200;
      const headers = path.startsWith('/assets/')
        ? {'Cache-Control': 'public, max-age=31536000, immutable'}
        : {
            'Content-Security-Policy': "default-src 'self'",
            'Content-Type': 'text/html; charset=utf-8',
          };
      return new Response(request.method === 'HEAD' ? null : body, {
        status,
        headers,
      });
    },
  };
}

test('public routes serve their own built pages through direct and clean URLs', async () => {
  const assetsBinding = assets({
    '/index.html': '<main>home</main>',
    '/privacy/index.html': '<main>privacy</main>',
    '/examples/example-1/index.html': '<main>example</main>',
  });
  const env = {ASSETS: assetsBinding};

  for (const [path, expectedPath, body] of [
    ['/', '/index.html', '<main>home</main>'],
    ['/?source=nav', '/index.html', '<main>home</main>'],
    ['/privacy', '/privacy/index.html', '<main>privacy</main>'],
    ['/privacy?language=en', '/privacy/index.html', '<main>privacy</main>'],
    ['/privacy/', '/privacy/index.html', '<main>privacy</main>'],
    ['/privacy/index.html', '/privacy/index.html', '<main>privacy</main>'],
    [
      '/examples/example-1',
      '/examples/example-1/index.html',
      '<main>example</main>',
    ],
    [
      '/examples/example-1/',
      '/examples/example-1/index.html',
      '<main>example</main>',
    ],
    [
      '/examples/example-1/index.html',
      '/examples/example-1/index.html',
      '<main>example</main>',
    ],
  ]) {
    const response = await worker.fetch(
      new Request(`https://open-coscientist.com${path}`),
      env,
    );
    assert.equal(response.status, 200, path);
    assert.equal(await response.text(), body, path);
    assert.ok(
      assetsBinding.requests.some(request => request.path === expectedPath),
      path,
    );
  }

  const head = await worker.fetch(
    new Request('https://open-coscientist.com/examples/example-1/?ref=search', {
      method: 'HEAD',
    }),
    env,
  );
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
  assert.deepEqual(assetsBinding.requests.at(-1), {
    path: '/examples/example-1/index.html',
    method: 'HEAD',
    search: '?ref=search',
  });

  const manifestAssets = assets({'/examples.json': '[{"id":"example-1"}]'});
  const manifest = await worker.fetch(
    new Request('https://open-coscientist.com/examples.json'),
    {ASSETS: manifestAssets},
  );
  assert.equal(await manifest.text(), '[{"id":"example-1"}]');
});

test('private workbench routes share a non-indexable fallback', async () => {
  const env = {ASSETS: assets({'/private.html': '<main>private shell</main>'})};

  for (const path of [
    '/chats/private-chat',
    '/runs/private-run/report',
    '/operations/spend',
  ]) {
    const response = await worker.fetch(
      new Request(`https://open-coscientist.com${path}?tab=details`),
      env,
    );
    assert.equal(response.status, 200, path);
    assert.equal(await response.text(), '<main>private shell</main>', path);
    assert.equal(
      response.headers.get('X-Robots-Tag'),
      'noindex, nofollow',
      path,
    );
    assert.equal(
      response.headers.get('Content-Type'),
      'text/html; charset=utf-8',
      path,
    );
    assert.equal(
      response.headers.get('Content-Security-Policy'),
      "default-src 'self'",
      path,
    );
    assert.ok(!response.headers.has('Link'), path);
  }

  const head = await worker.fetch(
    new Request('https://open-coscientist.com/chats/private-chat?tab=details', {
      method: 'HEAD',
    }),
    env,
  );
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
  assert.equal(head.headers.get('X-Robots-Tag'), 'noindex, nofollow');
  assert.equal(
    head.headers.get('Content-Security-Policy'),
    "default-src 'self'",
  );
});

test('missing public pages and unknown routes return a noindex 404, never home', async () => {
  const env = {
    ASSETS: assets({
      '/index.html': '<main>home</main>',
      '/404.html': '<main><h1>Page not found</h1></main>',
    }),
  };

  for (const path of ['/privacy', '/examples/not-curated', '/unknown']) {
    const response = await worker.fetch(
      new Request(`https://open-coscientist.com${path}`),
      env,
    );
    assert.equal(response.status, 404, path);
    assert.equal(
      response.headers.get('X-Robots-Tag'),
      'noindex, nofollow',
      path,
    );
    assert.equal(
      response.headers.get('Content-Type'),
      'text/html; charset=utf-8',
      path,
    );
    assert.equal(
      response.headers.get('Content-Security-Policy'),
      "default-src 'self'",
      path,
    );
    assert.equal(
      await response.text(),
      '<main><h1>Page not found</h1></main>',
      path,
    );
  }

  const head = await worker.fetch(
    new Request('https://open-coscientist.com/unknown', {method: 'HEAD'}),
    env,
  );
  assert.equal(head.status, 404);
  assert.equal(await head.text(), '');
  assert.equal(head.headers.get('X-Robots-Tag'), 'noindex, nofollow');
  assert.equal(
    head.headers.get('Content-Security-Policy'),
    "default-src 'self'",
  );
});

test('the worker is served only on its custom domain', () => {
  const config = JSON.parse(
    readFileSync(new URL('../../wrangler.jsonc', import.meta.url), 'utf8'),
  );
  assert.equal(config.workers_dev, false);
  assert.equal(config.preview_urls, false);
  assert.equal(config.assets.html_handling, 'none');
  assert.equal(config.assets.not_found_handling, 'none');
});

test('a missing bundle is never cached while present bundles stay immutable', async () => {
  const env = {ASSETS: assets({'/assets/app.js': 'asset'})};

  const missing = await worker.fetch(
    new Request('https://open-coscientist.com/assets/missing.js'),
    env,
  );
  const present = await worker.fetch(
    new Request('https://open-coscientist.com/assets/app.js'),
    env,
  );

  assert.equal(missing.status, 404);
  assert.equal(missing.headers.get('Cache-Control'), 'no-store');
  assert.equal(
    present.headers.get('Cache-Control'),
    'public, max-age=31536000, immutable',
  );
});

test('build prerenders the public fixture projection and discovery files', async t => {
  const dist = mkdtempSync(path.join(tmpdir(), 'cosci-prerender-'));
  t.after(() => rmSync(dist, {recursive: true, force: true}));
  const fallbackDist = mkdtempSync(
    path.join(tmpdir(), 'cosci-prerender-fallback-'),
  );
  t.after(() => rmSync(fallbackDist, {recursive: true, force: true}));
  const baseHtml = `<!doctype html>
<html><head>
<title>Open Co-Scientist</title>
<meta name="description" content="Original description" />
<meta name="robots" content="index, follow" />
<meta name="googlebot" content="index, follow" />
<link rel="canonical" href="https://open-coscientist.com" />
<meta property="og:title" content="Open Co-Scientist" />
<meta property="og:description" content="Original description" />
<meta property="og:url" content="https://open-coscientist.com" />
<meta name="twitter:title" content="Open Co-Scientist" />
<meta name="twitter:description" content="Original description" />
</head><body><div id="root"><p>Loading</p></div>
<noscript><main>Old copy</main></noscript></body></html>`;
  writeFileSync(path.join(dist, 'index.html'), baseHtml);

  const result = spawnSync(process.execPath, [prerenderScript], {
    cwd: path.resolve(frontendRoot, '../..'),
    env: {...process.env, COSCI_FRONTEND_DIST: dist},
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);

  const examples = JSON.parse(
    readFileSync(path.join(dist, 'examples.json'), 'utf8'),
  );
  assert.equal(examples.length, 3);
  assert.deepEqual(
    JSON.parse(
      readFileSync(path.join(frontendRoot, 'public/examples.json'), 'utf8'),
    ),
    examples,
  );
  const fixture = JSON.parse(
    gunzipSync(
      readFileSync(
        path.resolve(
          frontendRoot,
          '../../engine/src/co_scientist/domains/chat/data/demo_runs.json.gz',
        ),
      ),
    ).toString('utf8'),
  );
  const hypotheses = fixture.tables.hypotheses;
  const reportsByRun = new Map(
    fixture.tables.reports.map(report => [report.run_id, report]),
  );
  const expectedExamples = fixture.tables.runs.map(run => {
    const report = reportsByRun.get(run.id);
    assert.ok(report, `missing fixture report for ${run.id}`);
    const payload = JSON.parse(report.payload_json);
    const summary = payload.research_overview?.overview?.summary;
    assert.equal(typeof summary, 'string');
    const ideas = payload.leaderboard.slice(0, 3).map(item => {
      const hypothesis = hypotheses.find(
        row => row.id === item.id && row.run_id === run.id,
      );
      assert.ok(hypothesis, `missing leaderboard hypothesis ${item.id}`);
      return {title: item.title, statement: hypothesis.statement};
    });
    const heading = run.title.replace(/^Example:\s*/i, '').trim();
    const goal = run.research_goal.replace(/\s+/g, ' ').trim();
    return {
      id: run.id,
      title: `${heading} - Open Co-Scientist`,
      description: goal,
      heading,
      goal,
      lastmod: new Date(run.updated_at * 1000).toISOString().slice(0, 10),
      summary: summary.replace(/\s+/g, ' ').trim(),
      ideas,
    };
  });
  assert.deepEqual(examples, expectedExamples);
  assert.doesNotMatch(
    JSON.stringify(examples),
    /"(?:client_id|provider|payload_json|markdown_text|api_key)"/i,
  );
  for (const example of examples) {
    assert.deepEqual(Object.keys(example).sort(), [
      'description',
      'goal',
      'heading',
      'id',
      'ideas',
      'lastmod',
      'summary',
      'title',
    ]);
    assert.match(example.id, /^[0-9a-f-]{36}$/);
    assert.equal(example.title, `${example.heading} - Open Co-Scientist`);
    assert.equal(example.description, example.goal);
    assert.ok(!example.heading.startsWith('Example:'));
    assert.match(example.lastmod, /^\d{4}-\d{2}-\d{2}$/);
    assert.equal(typeof example.summary, 'string');
    assert.ok(example.summary.length > 0);
    assert.equal(example.ideas.length, 3);
    for (const idea of example.ideas) {
      assert.deepEqual(Object.keys(idea).sort(), ['statement', 'title']);
      assert.ok(idea.title.length > 0);
      assert.ok(idea.statement.length > 0);
    }

    const html = readFileSync(
      path.join(dist, 'examples', example.id, 'index.html'),
      'utf8',
    );
    assert.ok(html.includes(`<title>${example.title}</title>`));
    assert.ok(html.includes(`<h1>${example.heading}</h1>`));
    assert.ok(html.includes('<h2>Research goal</h2>'));
    assert.ok(html.includes(`<p>${example.goal}</p>`));
    assert.ok(html.includes('<h2>Example overview</h2>'));
    assert.ok(html.includes(`<p>${example.summary}</p>`));
    assert.ok(html.includes('<h2>Leading hypotheses</h2>'));
    assert.match(
      html,
      /<h2>Leading hypotheses<\/h2>[\s\S]*?<ol>[\s\S]*?<\/ol>/,
    );
    for (const idea of example.ideas) {
      assert.ok(html.includes(`<h3>${idea.title}</h3>`));
      assert.ok(html.includes(`<p>${idea.statement}</p>`));
    }
    assert.ok(
      html.includes(
        `<link rel="canonical" href="https://open-coscientist.com/examples/${example.id}"`,
      ),
    );
    assert.match(html, /<div id="root"><main/);
    assert.doesNotMatch(html, /<noscript>[\s\S]*?<h1>/);
  }

  const privacyHtml = readFileSync(
    path.join(dist, 'privacy/index.html'),
    'utf8',
  );
  assert.match(privacyHtml, /<h1>Privacy notice<\/h1>/);
  assert.match(
    privacyHtml,
    /Sentry uses the free Developer plan, with retention of 30 days for new error events\. Events ingested during the earlier free Business trial retain 90 days\./,
  );

  const sitemap = readFileSync(path.join(dist, 'sitemap.xml'), 'utf8');
  const sourceSitemap = readFileSync(
    path.join(frontendRoot, 'public/sitemap.xml'),
    'utf8',
  );
  const withoutLastmod = value =>
    value.replace(/<lastmod>\d{4}-\d{2}-\d{2}<\/lastmod>/g, '');
  assert.equal(withoutLastmod(sourceSitemap), withoutLastmod(sitemap));
  assert.match(
    sitemap,
    /<loc>https:\/\/open-coscientist\.com<\/loc>[\s\S]*?<lastmod>\d{4}-\d{2}-\d{2}<\/lastmod>/,
  );
  assert.match(
    sitemap,
    /<loc>https:\/\/open-coscientist\.com\/privacy<\/loc>\s*<lastmod>\d{4}-\d{2}-\d{2}<\/lastmod>/,
  );
  for (const example of examples) {
    assert.ok(
      sitemap.includes(`https://open-coscientist.com/examples/${example.id}`),
    );
    assert.ok(sitemap.includes(`<lastmod>${example.lastmod}</lastmod>`));
    assert.ok(sourceSitemap.includes(`<lastmod>${example.lastmod}</lastmod>`));
  }

  const llms = readFileSync(path.join(dist, 'llms.txt'), 'utf8');
  assert.equal(
    readFileSync(path.join(frontendRoot, 'public/llms.txt'), 'utf8'),
    llms,
  );
  assert.match(llms, /https:\/\/open-coscientist\.com\/privacy/);
  for (const example of examples) {
    assert.ok(
      llms.includes(`https://open-coscientist.com/examples/${example.id}`),
    );
    assert.ok(llms.includes(example.heading));
    assert.ok(llms.includes(example.summary));
    for (const idea of example.ideas) {
      assert.ok(llms.includes(idea.title));
      assert.ok(llms.includes(idea.statement));
    }
  }

  const privatePage = readFileSync(path.join(dist, 'private.html'), 'utf8');
  assert.match(privatePage, /name="robots" content="noindex, nofollow"/);
  assert.doesNotMatch(privatePage, /rel="canonical"/);
  assert.match(
    readFileSync(path.join(dist, '404.html'), 'utf8'),
    /name="robots" content="noindex, nofollow"/,
  );

  writeFileSync(path.join(fallbackDist, 'index.html'), baseHtml);
  const archiveBuild = spawnSync(process.execPath, [prerenderScript], {
    cwd: path.resolve(frontendRoot, '../..'),
    env: {...process.env, PATH: '', COSCI_FRONTEND_DIST: fallbackDist},
    encoding: 'utf8',
  });
  assert.equal(
    archiveBuild.status,
    0,
    archiveBuild.stderr || archiveBuild.stdout,
  );
  assert.equal(
    readFileSync(path.join(fallbackDist, 'sitemap.xml'), 'utf8'),
    sitemap,
  );
});
