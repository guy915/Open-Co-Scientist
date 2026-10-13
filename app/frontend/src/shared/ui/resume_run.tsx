import {useState} from 'react';
import {resumeRun} from '@/shared/api/runs';
import {errorMessage} from '@/shared/lib/errors';
import {RUNS_CHANGED_EVENT} from '@/shared/lib/dom_events';
import {Button, Dialog, DIALOG_TITLE_CLASSES, StatusText} from '@/shared/ui';

export function ResumeRunControl({
  runId,
  requiresAck,
  onResumed,
  compact = false,
}: {
  runId: string;
  requiresAck: boolean;
  onResumed: () => void;
  compact?: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function resume() {
    setBusy(true);
    setError(null);
    try {
      await resumeRun(runId);
      window.dispatchEvent(new Event(RUNS_CHANGED_EVENT));
      onResumed();
    } catch (cause) {
      setError(errorMessage(cause));
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        variant={compact ? 'tonal' : undefined}
        size={compact ? 'sm' : undefined}
        disabled={busy}
        onClick={() => {
          setError(null);
          if (requiresAck) setConfirming(true);
          else void resume();
        }}
      >
        Resume run
      </Button>
      {!requiresAck && error && <StatusText tone="danger">{error}</StatusText>}
      {requiresAck && (
        <Dialog
          open={confirming}
          onClose={() => !busy && setConfirming(false)}
          label="Resume interrupted run"
        >
          <h2 className={DIALOG_TITLE_CLASSES}>Resume interrupted run?</h2>
          <p className="m-0">
            Resuming from the saved checkpoint may repeat provider requests and
            charges, depending on your provider route. The interrupted
            request&apos;s acceptance is unknown. Completed stages will be kept.
          </p>
          {error && <StatusText tone="danger">{error}</StatusText>}
          <div className="flex justify-end gap-3">
            <Button
              variant="text"
              disabled={busy}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </Button>
            <Button disabled={busy} onClick={() => void resume()}>
              {busy ? 'Resuming…' : 'Confirm resume'}
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
}
