import {mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

// Absolute harness paths keep servers and tests aligned regardless of
// invocation cwd.
const HERE = dirname(fileURLToPath(import.meta.url));

export const REPO_ROOT = resolve(HERE, '..', '..');

export const APP_DIR = join(REPO_ROOT, 'app');

export const FRONTEND_DIR = join(APP_DIR, 'frontend');

export const VENV_PYTHON = join(REPO_ROOT, '.venv', 'bin', 'python');

// Use separate ports to avoid colliding with make start.
export const API_PORT = Number(process.env.COSCI_E2E_API_PORT ?? 8108);
export const UI_PORT = Number(process.env.COSCI_E2E_UI_PORT ?? 5273);

export const API_URL = `http://127.0.0.1:${API_PORT}`;
export const UI_URL = `http://127.0.0.1:${UI_PORT}`;

// The wrapper supplies one isolated directory; raw Playwright gets a fresh fallback.
export function runStateDir(): string {
  const existing = process.env.COSCI_E2E_STATE_DIR;
  if (existing) return existing;
  const dir = mkdtempSync(join(tmpdir(), 'cosci-e2e-'));
  process.env.COSCI_E2E_STATE_DIR = dir;
  return dir;
}
