import {removeStorage, STORAGE_KEYS} from '@/shared/lib/safe_storage';
import {sessionBaselineMemory} from './diagnostics_baseline_state';

export function resetSessionBaselineForTest(): void {
  sessionBaselineMemory.current = null;
  removeStorage('session', STORAGE_KEYS.logsBaseline);
}
