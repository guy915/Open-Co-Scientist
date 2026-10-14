import {spawnSync} from 'node:child_process';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const state = mkdtempSync(join(tmpdir(), 'cosci-e2e-'));
try {
  const result = spawnSync(
    process.env.BUN || 'bun',
    ['x', 'playwright', 'test', ...process.argv.slice(2)],
    {
      cwd: dirname(fileURLToPath(import.meta.url)),
      env: {...process.env, COSCI_E2E_STATE_DIR: state},
      stdio: 'inherit',
    },
  );
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally {
  // Playwright has now stopped its servers, including outstanding database writes.
  rmSync(state, {recursive: true, force: true});
}
