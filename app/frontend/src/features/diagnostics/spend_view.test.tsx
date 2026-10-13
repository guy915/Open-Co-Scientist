import {act, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, it, vi} from 'vitest';
import {getSpend, type SpendSnapshot} from '@/shared/api/spend';
import {SpendView} from './spend_view';

vi.mock('@/shared/api/spend', () => ({getSpend: vi.fn()}));
afterEach(() => vi.clearAllMocks());

it('keeps the token out of storage and hides old spend on a failed refresh', async () => {
  vi.mocked(getSpend).mockRejectedValue(new Error('not found'));
  const local = vi.spyOn(Storage.prototype, 'setItem');
  render(<SpendView />);
  expect(getSpend).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Operator token'), {
    target: {value: 'private-token'},
  });
  fireEvent.click(screen.getByRole('button', {name: 'Load spend'}));
  await waitFor(() =>
    expect(screen.getByRole('alert')).toHaveTextContent('not found'),
  );
  expect(getSpend).toHaveBeenCalledWith('private-token');
  expect(local).not.toHaveBeenCalled();
  expect(screen.queryByText('Azure')).not.toBeInTheDocument();
  local.mockRestore();
});

it('discards a reply after the operator changes the token', async () => {
  let finish: (result: SpendSnapshot) => void = () => {};
  vi.mocked(getSpend).mockReturnValue(
    new Promise(resolve => {
      finish = resolve;
    }),
  );
  render(<SpendView />);
  const input = screen.getByLabelText('Operator token');
  fireEvent.change(input, {target: {value: 'first'}});
  fireEvent.click(screen.getByRole('button', {name: 'Load spend'}));
  fireEvent.change(input, {target: {value: 'second'}});
  await act(async () => {
    finish({
      azure: {
        available: true,
        today_eur: 1,
        week_eur: 2,
        total_spent_eur: 3,
        reserved_eur: 4,
        run_forecasts_eur: 5,
        total_budget_eur: 100,
        remaining_eur: 88,
        burn_eur_per_day: 1,
        days_at_current_rate: 88,
      },
      anthropic: {
        available: true,
        cycle_spent_usd: 0,
        reserved_usd: 0,
        grant_usd: 100,
        remaining_credit_usd: 100,
        usable_allowance_usd: 95,
        reset_at: null,
      },
      cache_by_role: [],
    });
  });
  expect(screen.queryByText('Azure')).not.toBeInTheDocument();
  expect(screen.getByRole('button', {name: 'Load spend'})).toBeEnabled();
});
