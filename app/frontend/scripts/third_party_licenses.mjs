import {existsSync, readdirSync, readFileSync} from 'node:fs';
import {join, sep} from 'node:path';

// Minification strips every licence comment, so the bundle meets its MIT/BSD
// notice terms only through this served file.
const LICENSES_FILE = 'third-party-licenses.txt';

// Anything else needs a deliberate review before it reaches visitors.
const BUNDLE_LICENSES = new Set([
  '0BSD',
  'Apache-2.0',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'CC0-1.0',
  'ISC',
  'MIT',
  'MIT-0',
  'OFL-1.1',
  'Unlicense',
  'Zlib',
]);

// Build and test tools never reach the browser; they only must not carry a
// licence that restricts use or that we cannot identify.
const BLOCKED =
  /\bA?GPL|General Public|SSPL|Server Side Public|BUSL|Business Source|Elastic|Commons Clause|-NC\b|-ND\b|-NC-|-ND-|NonCommercial|NoDerivatives|UNLICENSED|SEE LICENSE/i;

const NOTICE_FILE = /^(licen[cs]e|copying|notice)(\.|-|$)/i;

export function licenseOf(manifest) {
  const value = manifest.license ?? manifest.licenses;
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && !Array.isArray(value))
    return value.type ?? '';
  if (Array.isArray(value))
    return value
      .map(entry => (typeof entry === 'string' ? entry : entry?.type))
      .filter(Boolean)
      .join(' OR ');
  return '';
}

function spdxIds(expression) {
  return expression
    .replace(/[()]/g, ' ')
    .split(/\s+(?:AND|OR)\s+|\s+/)
    .filter(token => token && token !== 'AND' && token !== 'OR');
}

export function bundleLicenseError(name, expression) {
  if (!expression) return `${name}: no licence declared`;
  const ids = spdxIds(expression);
  const allowed = expression.includes(' AND ')
    ? ids.every(id => BUNDLE_LICENSES.has(id))
    : ids.some(id => BUNDLE_LICENSES.has(id));
  return allowed ? undefined : `${name}: licence ${expression} is not allowed`;
}

export function toolLicenseError(name, expression) {
  if (!expression) return `${name}: no licence declared`;
  return BLOCKED.test(expression)
    ? `${name}: licence ${expression} is blocked`
    : undefined;
}

function packageRoots(nodeModules) {
  const roots = [];
  if (!existsSync(nodeModules)) return roots;
  const visit = dir => {
    for (const entry of readdirSync(dir, {withFileTypes: true})) {
      if (entry.name.startsWith('.') || !entry.isDirectory()) continue;
      const path = join(dir, entry.name);
      if (entry.name.startsWith('@')) {
        visit(path);
        continue;
      }
      if (existsSync(join(path, 'package.json'))) roots.push(path);
      const nested = join(path, 'node_modules');
      if (existsSync(nested)) roots.push(...packageRoots(nested));
    }
  };
  visit(nodeModules);
  return roots;
}

export function installedLicenseErrors(nodeModulesDirs) {
  const errors = [];
  for (const dir of nodeModulesDirs)
    for (const root of packageRoots(dir)) {
      const manifest = JSON.parse(
        readFileSync(join(root, 'package.json'), 'utf8'),
      );
      // Nested package.json files that only set "type" are not packages.
      if (!manifest.name) continue;
      const error = toolLicenseError(
        `${manifest.name}@${manifest.version}`,
        licenseOf(manifest),
      );
      if (error) errors.push(error);
    }
  return errors;
}

export function packageRootOf(id) {
  // Plugin-wrapped modules carry a NUL prefix and query suffix.
  const parts = id.replace(/^\0/, '').split('?')[0].split(/[\\/]/);
  const at = parts.lastIndexOf('node_modules');
  if (at < 0 || at + 1 >= parts.length) return undefined;
  const length = parts[at + 1].startsWith('@') ? 2 : 1;
  return parts.slice(0, at + 1 + length).join(sep);
}

export function renderNotices(packageRoots) {
  const errors = [];
  const sections = [];
  const seen = new Set();
  const manifests = [...packageRoots]
    .map(root => ({
      root,
      manifest: JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')),
    }))
    .sort((a, b) => a.manifest.name.localeCompare(b.manifest.name));
  for (const {root, manifest} of manifests) {
    const id = `${manifest.name}@${manifest.version}`;
    if (seen.has(id)) continue;
    seen.add(id);
    const license = licenseOf(manifest);
    const error = bundleLicenseError(id, license);
    if (error) errors.push(error);
    const texts = readdirSync(root)
      .filter(file => NOTICE_FILE.test(file))
      .sort()
      .map(file => readFileSync(join(root, file), 'utf8').trim());
    if (!texts.length) errors.push(`${id}: ships no licence file`);
    const repository =
      typeof manifest.repository === 'string'
        ? manifest.repository
        : (manifest.repository?.url ?? '');
    sections.push(
      [
        '-'.repeat(72),
        `${id}`,
        `License: ${license}`,
        ...(repository ? [`Source: ${repository}`] : []),
        '',
        ...texts.flatMap(text => [text, '']),
      ].join('\n'),
    );
  }
  const text = [
    'Third-party software in the Co-Scientist web client',
    '',
    'The scripts, styles and fonts served by this site include the packages',
    'below. Each is listed with its licence and the full licence and notice',
    'text it ships with.',
    '',
    ...sections,
  ].join('\n');
  return {text, errors};
}

// Material Symbols paths are copied into source and Tailwind's theme CSS is
// inlined by its own plugin, so the module graph sees neither.
const UNTRACKED_PACKAGES = ['@material-symbols/svg-400', 'tailwindcss'];

export function thirdPartyLicenses({root}) {
  const nodeModules = join(root, 'node_modules');
  return {
    name: 'third-party-licenses',
    apply: 'build',
    buildStart() {
      const errors = installedLicenseErrors([
        nodeModules,
        join(root, '../../e2e/node_modules'),
      ]);
      if (errors.length) this.error(errors.join('\n'));
    },
    generateBundle(_options, bundle) {
      const roots = new Set(UNTRACKED_PACKAGES.map(name => join(nodeModules, name)));
      const add = id => {
        const packageRoot = packageRootOf(id);
        if (packageRoot) roots.add(packageRoot);
      };
      for (const output of Object.values(bundle)) {
        if (output.type === 'chunk') Object.keys(output.modules).forEach(add);
        else (output.originalFileNames ?? []).forEach(add);
      }
      // Fonts reach the output through CSS url() rather than as modules.
      for (const id of this.getModuleIds())
        if (/\.css(\?|$)/.test(id)) add(id);
      const {text, errors} = renderNotices(roots);
      if (errors.length) this.error(errors.join('\n'));
      this.emitFile({type: 'asset', fileName: LICENSES_FILE, source: text});
    },
  };
}
