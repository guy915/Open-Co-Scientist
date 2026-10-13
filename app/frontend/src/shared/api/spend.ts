import {fetchJson} from './runs';

export interface SpendSnapshot {
  azure: {
    available: boolean;
    today_eur: number;
    week_eur: number;
    total_spent_eur: number;
    reserved_eur: number;
    run_forecasts_eur: number;
    total_budget_eur: number | null;
    remaining_eur: number | null;
    burn_eur_per_day: number;
    days_at_current_rate: number | null;
  };
  anthropic: {
    available: boolean;
    cycle_spent_usd: number;
    reserved_usd: number;
    grant_usd: number | null;
    remaining_credit_usd: number | null;
    usable_allowance_usd: number | null;
    reset_at: number | null;
  };
  cache_by_role: {
    currency: 'EUR' | 'USD';
    role: string;
    calls: number;
    prompt_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    cost: number;
  }[];
}

export function getSpend(token: string): Promise<SpendSnapshot> {
  return fetchJson('/api/spend', {
    headers: {'X-Logs-Token': token},
    cache: 'no-store',
  });
}
