import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {gzipSync} from 'node:zlib';
import {measureBundles} from './check-bundle.mjs';
import {execFile} from 'node:child_process';
import {readFile, mkdir} from 'node:fs/promises';
import {promisify} from 'node:util';
import {fileURLToPath} from 'node:url';

test('CLI fails for actual growth and missing build artifacts, without installed dependencies', async () => {
  const dist = await mkdtemp(join(tmpdir(), 'bundle cli '));
  const script = fileURLToPath(new URL('./check-bundle.mjs', import.meta.url));
  const budgets = JSON.parse(
    await readFile(new URL('./bundle-budget.json', import.meta.url), 'utf8'),
  );
  const run = () =>
    promisify(execFile)(process.execPath, [script], {
      env: {...process.env, COSCI_FRONTEND_DIST: dist},
    });
  try {
    await assert.rejects(
      run(),
      error => error.code === 1 && error.stderr.includes('manifest.json'),
    );
    await mkdir(join(dist, '.vite'));
    const manifest = Object.fromEntries(
      Object.values(budgets)
        .flatMap(budget => budget.entries)
        .map(entry => [entry, {file: 'entry.js', css: ['style.css']}]),
    );
    await writeFile(
      join(dist, '.vite/manifest.json'),
      JSON.stringify(manifest),
    );
    await writeFile(join(dist, 'entry.js'), 'console.log(1)');
    await writeFile(join(dist, 'style.css'), 'body{}');
    assert.match((await run()).stdout, /PASS home/);
    let seed = 42;
    const growth = Buffer.alloc(500000);
    for (let i = 0; i < growth.length; i++) {
      seed ^= seed << 13;
      seed ^= seed >>> 17;
      seed ^= seed << 5;
      growth[i] = 33 + ((seed >>> 0) % 94);
    }
    await writeFile(join(dist, 'entry.js'), growth);
    await assert.rejects(
      run(),
      error => error.code === 1 && error.stdout.includes('FAIL home'),
    );
    await rm(join(dist, 'entry.js'));
    await assert.rejects(
      run(),
      error => error.code === 1 && error.stderr.includes('entry.js'),
    );
  } finally {
    await rm(dist, {recursive: true, force: true});
  }
});

test('counts shared and cyclic static dependencies once and excludes unused dynamic imports', async () => {
  const dist = await mkdtemp(join(tmpdir(), 'bundle-check-'));
  try {
    const files = {
      'entry.js': 'entry',
      'shared.js': 'shared',
      'report.js': 'report',
      'style.css': 'style',
    };
    for (const [file, body] of Object.entries(files))
      await writeFile(join(dist, file), body);
    const manifest = {
      entry: {
        file: 'entry.js',
        imports: ['shared'],
        dynamicImports: ['report'],
        css: ['style.css'],
      },
      shared: {file: 'shared.js', imports: ['entry'], css: ['style.css']},
      report: {file: 'report.js', imports: ['entry', 'shared']},
    };
    await assert.rejects(measureBundles(dist, {}, {}), /No bundle budgets/);
    await assert.rejects(
      measureBundles(dist, {}, {home: {entries: ['entry'], js: -1, css: 1000}}),
      /invalid bundle budget/,
    );
    const [home, report] = await measureBundles(dist, manifest, {
      home: {entries: ['entry'], js: 1000, css: 1000},
      report: {entries: ['entry', 'report'], js: 1, css: 1000},
    });
    const gzip = body => gzipSync(body, {level: 9}).length;
    assert.equal(home.js, gzip('entry') + gzip('shared'));
    assert.equal(home.css, gzip('style'));
    assert.equal(home.passed, true);
    assert.equal(report.js, home.js + gzip('report'));
    assert.equal(report.passed, false);
    await assert.rejects(
      measureBundles(dist, manifest, {
        missing: {entries: ['missing'], js: 1000, css: 1000},
      }),
      /missing manifest entry/,
    );
    await assert.rejects(
      measureBundles(
        dist,
        {entry: {file: '../outside.js'}},
        {home: {entries: ['entry'], js: 1000, css: 1000}},
      ),
      /outside dist/,
    );
    await assert.rejects(
      measureBundles(
        dist,
        {...manifest, unbudgeted: {file: 'report.js', isDynamicEntry: true}},
        {home: {entries: ['entry'], js: 1000, css: 1000}},
      ),
      /Declare a bundle budget/,
    );
    const [withTelemetry] = await measureBundles(
      dist,
      {
        ...manifest,
        'src/shared/lib/error_tracking.ts': {
          file: 'report.js',
          isDynamicEntry: true,
        },
      },
      {home: {entries: ['entry'], js: 1000, css: 1000}},
    );
    assert.equal(withTelemetry.js, home.js + gzip('report'));
  } finally {
    await rm(dist, {recursive: true, force: true});
  }
});
