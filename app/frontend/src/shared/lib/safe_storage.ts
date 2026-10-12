// Every browser storage key the app persists. Users' browsers already hold
// these names, so they are never renamed.
export const STORAGE_KEYS = {
  clientId: 'co_scientist_client_id',
  theme: 'cosci-theme',
  apiKeys: 'cosci-api-keys',
  legacyApiKey: 'cosci-api-key',
  apiProvider: 'cosci-api-provider',
  workerModel: 'cosci-api-model',
  supervisorModel: 'cosci-api-supervisor-model',
  customModels: 'cosci-api-custom-models',
  sessionSidePrefix: 'cosci:session-side:',
  lastSessionSide: 'cosci:session-side',
  sessionTabPrefix: 'cosci:session-tab:',
  logsBaseline: 'cosci-logs-session-baseline',
  logsPauseUntil: 'co_scientist_log_pause_until',
  pendingRunCreatePrefix: 'co_scientist_pending_run_create:',
  chunkReloadAt: 'coscientist:chunk-reload-at',
} as const;

export type StorageArea = 'local' | 'session';

// Blocked site data, a full quota and some webviews make storage throw on
// access as well as on writes. These helpers never throw: reads return null
// and writes report whether they stuck.
function area(kind: StorageArea): Storage | null {
  try {
    return kind === 'local' ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

export function readStorage(kind: StorageArea, key: string): string | null {
  try {
    return area(kind)?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function writeStorage(
  kind: StorageArea,
  key: string,
  value: string,
): boolean {
  try {
    const store = area(kind);
    if (!store) return false;
    store.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function removeStorage(kind: StorageArea, key: string): boolean {
  try {
    const store = area(kind);
    if (!store) return false;
    store.removeItem(key);
    return true;
  } catch {
    return false;
  }
}
