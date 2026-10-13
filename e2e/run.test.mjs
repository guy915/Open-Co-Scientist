import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {
  chmodSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {test} from 'node:test';

for (const status of [0, 7]) {
  test(`cleanup waits for the child and preserves exit ${status}`, () => {
    const root = mkdtempSync(join(tmpdir(), 'cosci-runner-test-'));
    try {
      const fake = join(root, 'fake-bun');
      const record = join(root, 'record.json');
      writeFileSync(
        fake,
        `#!/usr/bin/env node
const fs = require('node:fs');
setTimeout(() => {
  const state = process.env.COSCI_E2E_STATE_DIR;
  fs.writeFileSync(state + '/late-write', 'background write before server exit');
  fs.writeFileSync(process.env.RUNNER_RECORD, JSON.stringify({state, args: process.argv.slice(2)}));
  process.exit(${status});
}, 20);
`,
      );
      chmodSync(fake, 0o755);
      const result = spawnSync(
        process.execPath,
        [
          fileURLToPath(new URL('./run.mjs', import.meta.url)),
          '--config',
          'custom.config.ts',
        ],
        {
          env: {
            ...process.env,
            BUN: fake,
            RUNNER_RECORD: record,
            COSCI_E2E_STATE_DIR: root,
          },
          encoding: 'utf8',
        },
      );
      assert.equal(result.status, status, result.stderr);
      const saved = JSON.parse(readFileSync(record, 'utf8'));
      assert.deepEqual(saved.args, [
        'x',
        'playwright',
        'test',
        '--config',
        'custom.config.ts',
      ]);
      assert.equal(existsSync(saved.state), false);
      assert.equal(existsSync(root), true);
    } finally {
      rmSync(root, {recursive: true, force: true});
    }
  });
}
