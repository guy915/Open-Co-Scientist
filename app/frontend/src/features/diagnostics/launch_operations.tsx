import {useState, type FormEvent} from 'react';
import {
  getLaunchControl,
  putLaunchControl,
  type LaunchControl,
} from '@/shared/api/launch_control';
import {Button, Card, TextArea, TextField} from '@/shared/ui';

type Mode = 'open' | 'finish' | 'cancel';

export function LaunchOperations() {
  const [token, setToken] = useState('');
  const [control, setControl] = useState<LaunchControl | null>(null);
  const [mode, setMode] = useState<Mode>('finish');
  const [message, setMessage] = useState('Research is temporarily paused.');
  const [returnTime, setReturnTime] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState('');

  async function load() {
    setBusy(true);
    setResult('');
    try {
      const next = await getLaunchControl(token);
      setControl(next);
      setMode(next.paused ? (next.drain ? 'cancel' : 'finish') : 'open');
      setMessage(next.message);
      setReturnTime('');
      setResult(`Loaded revision ${next.revision}.`);
    } catch (error) {
      setControl(null);
      setResult(
        error instanceof Error ? error.message : 'Control unavailable.',
      );
    } finally {
      setBusy(false);
    }
  }

  async function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!control || busy) return;
    setBusy(true);
    try {
      const next = await putLaunchControl(token, {
        paused: mode !== 'open',
        drain: mode === 'cancel',
        message,
        resumes_at:
          mode !== 'open' && returnTime
            ? new Date(returnTime).getTime() / 1000
            : null,
        expected_revision: control.revision,
      });
      // Reload the private credit summary; do not turn missing balances into zero.
      setControl(await getLaunchControl(token));
      setResult(
        mode === 'open'
          ? 'New work is enabled.'
          : mode === 'cancel'
            ? `New work is paused. Cancelled ${next.cancelled_runs ?? 0} runs; replies are stopping.`
            : 'New work is paused. Work already admitted can finish.',
      );
    } catch (error) {
      setResult(error instanceof Error ? error.message : 'Update failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mx-auto grid max-w-2xl gap-5 p-6 phone:p-4">
      <h1 className="m-0 text-2xl">Launch operations</h1>
      <p className="m-0">
        Only the configured operator token authorizes changes. It stays in
        memory until this page closes.
      </p>
      <Card size="panel">
        <label htmlFor="operator-token">Operator token</label>
        <TextField
          id="operator-token"
          type="password"
          autoComplete="off"
          value={token}
          onChange={event => {
            setToken(event.target.value);
            setControl(null);
          }}
        />
        <Button
          onClick={() => void load()}
          disabled={!token || busy}
          layoutClassName="mt-3"
        >
          Load control
        </Button>
      </Card>
      {control && (
        <form onSubmit={event => void apply(event)} className="grid gap-4">
          <fieldset
            disabled={busy}
            className="grid gap-3 rounded-xl border border-cosci-border p-4"
          >
            <legend>New work</legend>
            {(
              [
                ['open', 'Allow new runs and replies'],
                ['finish', 'Pause new work; let current work finish'],
                [
                  'cancel',
                  'Pause new work and cancel current runs and replies',
                ],
              ] as const
            ).map(([value, label]) => (
              <label key={value} className="flex items-start gap-2">
                <input
                  type="radio"
                  name="mode"
                  value={value}
                  checked={mode === value}
                  onChange={() => setMode(value)}
                />
                {label}
              </label>
            ))}
          </fieldset>
          <label htmlFor="launch-message">Visitor message</label>
          <TextArea
            id="launch-message"
            value={message}
            maxLength={400}
            required
            disabled={busy || mode === 'open'}
            onChange={event => setMessage(event.target.value)}
          />
          <label htmlFor="launch-return">
            Expected return (optional, your local time)
          </label>
          <TextField
            id="launch-return"
            type="datetime-local"
            value={returnTime}
            disabled={busy || mode === 'open'}
            onChange={event => setReturnTime(event.target.value)}
          />
          <p className="m-0 text-cosci-muted">
            An expected time informs visitors. You must explicitly enable new
            work again. Cancellation stops work cooperatively and does not
            refund uncertain provider spend.
          </p>
          <Button type="submit" disabled={busy || !message.trim()}>
            Apply control
          </Button>
          {control.backup && (
            <p className="m-0">
              Backups:{' '}
              {control.backup.enabled
                ? (control.backup.verification?.status ?? 'not yet verified')
                : 'not configured'}
              . Last verified:{' '}
              {control.backup.verification?.verified_at === undefined
                ? 'unknown'
                : new Date(
                    control.backup.verification.verified_at * 1000,
                  ).toLocaleString()}
              .{' '}
              {control.backup.verification?.status === 'failed' &&
                'Verify recovery before resuming new work.'}
            </p>
          )}
          {control.credit?.enabled && (
            <p className="m-0">
              Azure: {control.credit.available ? 'available' : 'unavailable'}.
              Charged and reserved:{' '}
              {control.credit.charged_and_reserved_microeur === null
                ? 'unknown'
                : `€${(control.credit.charged_and_reserved_microeur / 1_000_000).toFixed(2)}`}
              .
            </p>
          )}
        </form>
      )}
      <p
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="m-0 break-words"
      >
        {result}
      </p>
    </section>
  );
}
