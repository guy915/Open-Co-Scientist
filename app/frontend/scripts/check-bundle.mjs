import {readFile} from 'node:fs/promises';
import {resolve, dirname, sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {gzipSync} from 'node:zlib';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export async function measureBundles(dist, manifest, budgets) {
  if (!Object.keys(budgets).length)
    throw new Error('No bundle budgets declared');
  for (const [route, budget] of Object.entries(budgets)) {
    if (
      !Array.isArray(budget.entries) ||
      !budget.entries.length ||
      !budget.entries.every(entry => typeof entry === 'string') ||
      !Number.isSafeInteger(budget.js) ||
      budget.js < 1 ||
      !Number.isSafeInteger(budget.css) ||
      budget.css < 1
    )
      throw new Error(`${route}: invalid bundle budget`);
  }
  const covered = new Set(
    Object.values(budgets).flatMap(budget => budget.entries),
  );
  covered.add('src/shared/lib/error_tracking.ts');
  for (const [key, chunk] of Object.entries(manifest)) {
    if (chunk.isDynamicEntry && !covered.has(key))
      throw new Error(`Declare a bundle budget for dynamic entry ${key}`);
  }
  const sizes = new Map();
  async function size(file) {
    const path = resolve(dist, file);
    if (!path.startsWith(resolve(dist) + sep))
      throw new Error(`Asset outside dist: ${file}`);
    if (!sizes.has(file))
      sizes.set(file, gzipSync(await readFile(path), {level: 9}).length);
    return sizes.get(file);
  }
  const rows = [];
  for (const [route, budget] of Object.entries(budgets)) {
    const visited = new Set();
    const files = new Set();
    function visit(key) {
      if (visited.has(key)) return;
      const chunk = manifest[key];
      if (!chunk) throw new Error(`${route}: missing manifest entry ${key}`);
      visited.add(key);
      files.add(chunk.file);
      for (const css of chunk.css || []) files.add(css);
      for (const imported of chunk.imports || []) visit(imported);
    }
    for (const entry of budget.entries) visit(entry);
    // A configured DSN starts this dynamic import on entry, before interaction.
    if (manifest['src/shared/lib/error_tracking.ts'])
      visit('src/shared/lib/error_tracking.ts');
    const bytes = {js: 0, css: 0};
    for (const file of files) {
      const type = file.endsWith('.js')
        ? 'js'
        : file.endsWith('.css')
          ? 'css'
          : undefined;
      if (type) bytes[type] += await size(file);
    }
    rows.push({
      route,
      ...bytes,
      js_budget: budget.js,
      css_budget: budget.css,
      passed: bytes.js <= budget.js && bytes.css <= budget.css,
      files: [...files].sort(),
    });
  }
  return rows;
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  try {
    const dist = resolve(frontend, process.env.COSCI_FRONTEND_DIST || 'dist');
    const manifest = JSON.parse(
      await readFile(resolve(dist, '.vite/manifest.json'), 'utf8'),
    );
    const budgets = JSON.parse(
      await readFile(resolve(frontend, 'scripts/bundle-budget.json'), 'utf8'),
    );
    const rows = await measureBundles(dist, manifest, budgets);
    for (const row of rows)
      console.log(
        `${row.passed ? 'PASS' : 'FAIL'} ${row.route}: JS ${row.js}/${row.js_budget}, CSS ${row.css}/${row.css_budget} gzip bytes`,
      );
    if (rows.some(row => !row.passed)) process.exitCode = 1;
  } catch (error) {
    console.error(`Bundle check failed: ${error.message}`);
    process.exitCode = 1;
  }
}
