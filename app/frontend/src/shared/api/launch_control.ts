import {clientHeaders, fetchJson} from './runs';

export interface LaunchStatus {
  reason: 'paused' | 'free_capacity' | 'credit_exhausted' | null;
  message: string | null;
  resumes_at: number | null;
  paused: boolean;
  free_runs_allowed: boolean;
  byok_runs_allowed: boolean;
}

export interface LaunchControl {
  paused: boolean;
  drain: boolean;
  message: string;
  resumes_at: number | null;
  revision: number;
  credit?: {
    enabled: boolean;
    available: boolean;
    charged_and_reserved_microeur: number | null;
    total_microeur: number | null;
  };
  backup?: {
    enabled: boolean;
    verification: {
      status: 'verified' | 'failed';
      verified_at?: number;
      snapshot_at?: number;
      failed_at?: number;
      seconds?: number;
      txid?: string;
    } | null;
  };
  cancelled_runs?: number;
}

export function getLaunchStatus(): Promise<LaunchStatus> {
  return fetchJson('/api/launch-status', {headers: clientHeaders()});
}

// The operator secret is held by the page in memory, never URL or storage.
export function getLaunchControl(token: string): Promise<LaunchControl> {
  return fetchJson('/api/launch-control', {
    headers: {'X-Logs-Token': token},
  });
}

export function putLaunchControl(
  token: string,
  control: Omit<
    LaunchControl,
    'revision' | 'credit' | 'backup' | 'cancelled_runs'
  > & {
    expected_revision: number;
  },
): Promise<LaunchControl> {
  return fetchJson('/api/launch-control', {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      'X-Logs-Token': token,
    },
    body: JSON.stringify(control),
  });
}
