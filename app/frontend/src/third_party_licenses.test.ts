import {mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, sep} from 'node:path';
import {afterEach, describe, expect, it} from 'vitest';
import {
  bundleLicenseError,
  installedLicenseErrors,
  licenseOf,
  packageRootOf,
  renderNotices,
  toolLicenseError,
} from '../scripts/third_party_licenses.mjs';

const dirs: string[] = [];
afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, {recursive: true});
});

function nodeModules(
  packages: Record<string, {manifest: object; files?: Record<string, string>}>,
) {
  const dir = mkdtempSync(join(tmpdir(), 'licenses-'));
  dirs.push(dir);
  for (const [name, {manifest, files = {}}] of Object.entries(packages)) {
    const root = join(dir, 'node_modules', name);
    mkdirSync(root, {recursive: true});
    writeFileSync(join(root, 'package.json'), JSON.stringify(manifest));
    for (const [file, text] of Object.entries(files))
      writeFileSync(join(root, file), text);
  }
  return join(dir, 'node_modules');
}

describe('bundle licences', () => {
  it('accepts permissive licences and any allowed choice of an OR', () => {
    expect(bundleLicenseError('a', 'MIT')).toBeUndefined();
    expect(bundleLicenseError('a', '(MIT OR GPL-3.0-only)')).toBeUndefined();
    expect(bundleLicenseError('a', 'Apache-2.0 AND MIT')).toBeUndefined();
  });

  it('rejects copyleft, missing and unreviewed licences', () => {
    expect(bundleLicenseError('a', 'GPL-3.0-only')).toMatch(/not allowed/);
    expect(bundleLicenseError('a', 'MIT AND MPL-2.0')).toMatch(/not allowed/);
    expect(bundleLicenseError('a', 'CC-BY-NC-4.0')).toMatch(/not allowed/);
    expect(bundleLicenseError('a', '')).toMatch(/no licence/);
  });

  it('reads legacy licence fields', () => {
    expect(licenseOf({license: {type: 'MIT'}})).toBe('MIT');
    expect(licenseOf({licenses: [{type: 'MIT'}, {type: 'Apache-2.0'}]})).toBe(
      'MIT OR Apache-2.0',
    );
    expect(licenseOf({})).toBe('');
  });

  it('maps bundled module ids to their package root', () => {
    expect(
      packageRootOf(['', 'x', 'node_modules', 'react', 'index.js'].join(sep)),
    ).toBe(['', 'x', 'node_modules', 'react'].join(sep));
    expect(
      packageRootOf(
        ['', 'x', 'node_modules', '@sentry', 'core', 'a.js'].join(sep),
      ),
    ).toBe(['', 'x', 'node_modules', '@sentry', 'core'].join(sep));
    expect(packageRootOf('/x/src/main.tsx')).toBeUndefined();
  });

  it('writes every licence text and fails a package that ships none', () => {
    const dir = nodeModules({
      ok: {
        manifest: {name: 'ok', version: '1.0.0', license: 'MIT'},
        files: {LICENSE: 'Copyright OK Authors', 'license.md': 'second'},
      },
      bare: {manifest: {name: 'bare', version: '2.0.0', license: 'ISC'}},
    });
    const {text, errors} = renderNotices([join(dir, 'ok'), join(dir, 'bare')]);
    expect(text).toContain('ok@1.0.0\nLicense: MIT');
    expect(text).toContain('Copyright OK Authors');
    expect(text).toContain('second');
    expect(errors).toEqual(['bare@2.0.0: ships no licence file']);
  });
});

describe('installed tool licences', () => {
  it('blocks network copyleft, source-available and unlicensed tools', () => {
    for (const license of [
      'AGPL-3.0-only',
      'SSPL-1.0',
      'BUSL-1.1',
      'Elastic-2.0',
      'CC-BY-NC-SA-4.0',
      'UNLICENSED',
      'SEE LICENSE IN LICENSE.md',
    ])
      expect(toolLicenseError('a', license)).toMatch(/blocked/);
    expect(toolLicenseError('a', '')).toMatch(/no licence/);
    expect(toolLicenseError('a', 'MPL-2.0')).toBeUndefined();
    expect(toolLicenseError('a', 'CC-BY-4.0')).toBeUndefined();
    expect(toolLicenseError('a', 'Unlicense')).toBeUndefined();
  });

  it('scans scoped and nested packages', () => {
    const dir = nodeModules({
      '@scope/ok': {
        manifest: {name: '@scope/ok', version: '1.0.0', license: 'MIT'},
      },
      'ok/node_modules/inner': {
        manifest: {name: 'inner', version: '1.0.0', license: 'AGPL-3.0'},
      },
    });
    writeFileSync(
      join(dir, 'ok', 'package.json'),
      JSON.stringify({name: 'ok', version: '1.0.0', license: 'MIT'}),
    );
    expect(installedLicenseErrors([dir, join(dir, 'missing')])).toEqual([
      'inner@1.0.0: licence AGPL-3.0 is blocked',
    ]);
  });

  it('passes the installed frontend dependencies', () => {
    expect(
      installedLicenseErrors([join(__dirname, '../node_modules')]),
    ).toEqual([]);
  });
});
