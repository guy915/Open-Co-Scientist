import {useRef, useState} from 'react';
import {getSpend, type SpendSnapshot} from '@/shared/api/spend';
import {Button, Card, TextField} from '@/shared/ui';

function money(value: number | null, currency: 'EUR' | 'USD') {
  return value === null
    ? 'Not configured'
    : new Intl.NumberFormat(undefined, {style: 'currency', currency}).format(
        value,
      );
}

export function SpendView() {
  const [token, setToken] = useState('');
  const [snapshot, setSnapshot] = useState<SpendSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);

  async function load() {
    const current = ++generation.current;
    setBusy(true);
    setError('');
    setSnapshot(null);
    try {
      const result = await getSpend(token);
      if (generation.current === current) setSnapshot(result);
    } catch (failure) {
      if (generation.current === current) {
        setError(
          failure instanceof Error ? failure.message : 'Spend unavailable.',
        );
      }
    } finally {
      if (generation.current === current) setBusy(false);
    }
  }

  const azure = snapshot?.azure;
  const claude = snapshot?.anthropic;
  return (
    <section className="mx-auto grid max-w-3xl gap-5 p-6 phone:p-4">
      <h1 className="m-0 text-2xl">Model spend</h1>
      <Card size="panel">
        <label htmlFor="spend-token">Operator token</label>
        <TextField
          id="spend-token"
          type="password"
          autoComplete="off"
          value={token}
          onChange={event => {
            generation.current++;
            setToken(event.target.value);
            setSnapshot(null);
            setBusy(false);
            setError('');
          }}
        />
        <Button
          onClick={() => void load()}
          disabled={!token || busy}
          layoutClassName="mt-3"
        >
          {busy ? 'Loading…' : 'Load spend'}
        </Button>
        <p className="mb-0 text-cosci-muted">
          The token stays in this page's memory.
        </p>
      </Card>
      {error && <p role="alert">{error}</p>}
      {azure && (
        <Card size="panel">
          <h2>Azure</h2>
          <p>Slot {azure.available ? 'available' : 'unavailable'}.</p>
          <dl className="grid grid-cols-2 gap-3 phone:grid-cols-1">
            {(
              [
                ['Today (UTC)', money(azure.today_eur, 'EUR')],
                ['This week (from Monday UTC)', money(azure.week_eur, 'EUR')],
                ['Total spent', money(azure.total_spent_eur, 'EUR')],
                ['Calls with unknown usage', money(azure.reserved_eur, 'EUR')],
                [
                  'Admitted run forecasts',
                  money(azure.run_forecasts_eur, 'EUR'),
                ],
                ['Hard total', money(azure.total_budget_eur, 'EUR')],
                ['Remaining after holds', money(azure.remaining_eur, 'EUR')],
                [
                  'Burn per day (last 7 days)',
                  money(azure.burn_eur_per_day, 'EUR'),
                ],
                [
                  'Days at that rate',
                  azure.days_at_current_rate === null
                    ? 'No rate yet'
                    : azure.days_at_current_rate.toFixed(1),
                ],
              ] as const
            ).map(([label, value]) => (
              <div key={label}>
                <dt className="text-cosci-muted">{label}</dt>
                <dd className="m-0">{value}</dd>
              </div>
            ))}
          </dl>
        </Card>
      )}
      {claude && (
        <Card size="panel">
          <h2>Claude API credit</h2>
          <p>
            Spent this cycle: {money(claude.cycle_spent_usd, 'USD')}. Remaining
            credit after holds: {money(claude.remaining_credit_usd, 'USD')}.
          </p>
          <p>
            Unknown usage: {money(claude.reserved_usd, 'USD')}. Usable before
            the 95% stop: {money(claude.usable_allowance_usd, 'USD')}.
          </p>
          <p>
            Reset:{' '}
            {claude.reset_at === null
              ? 'Not configured'
              : new Date(claude.reset_at * 1000).toLocaleString()}
            . Slot {claude.available ? 'available' : 'unavailable'}.
          </p>
          <p className="text-cosci-muted">
            This is the app's ledger. Other Console usage can reduce the
            provider balance.
          </p>
        </Card>
      )}
      {snapshot && snapshot.cache_by_role.length > 0 && (
        <Card size="panel">
          <h2>Cache usage by call type</h2>
          <ul className="grid gap-3 pl-5">
            {snapshot.cache_by_role.map(row => (
              <li key={`${row.currency}:${row.role}`}>
                {row.role} ({row.currency}): {row.calls} calls;{' '}
                {row.prompt_tokens.toLocaleString()} prompt tokens;{' '}
                {row.cache_read_tokens.toLocaleString()} read;{' '}
                {row.cache_write_tokens.toLocaleString()} write;{' '}
                {money(row.cost, row.currency)}.
              </li>
            ))}
          </ul>
        </Card>
      )}
    </section>
  );
}
