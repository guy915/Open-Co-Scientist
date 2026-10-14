import {useEffect, useRef, useState} from 'react';
import {
  cancelRun,
  getRun,
  HttpError,
  isStoppableStatus,
  type RunStatus,
} from '@/shared/api/runs';
import type {IconName} from '@/shared/ui/icon';
import {logUiError} from '@/shared/lib/ui_logging';
import {Button} from '@/shared/ui';
import {ResumeRunControl} from '@/shared/ui/resume_run';
import {RUNS_CHANGED_EVENT} from '@/shared/lib/dom_events';

// Cancellation is terminal and cannot resume; require a second click without
// adding another modal.
const ARMED_MS = 4_000;

// A stale-status 409 means the run already ended; refreshing removes the
// control without reporting an error.
async function stopRun(runId: string): Promise<void> {
  try {
    await cancelRun(runId);
  } catch (error) {
    if (!(error instanceof HttpError && error.status === 409)) {
      logUiError('Failed to stop the run', String(error));
    }
  } finally {
    window.dispatchEvent(new Event(RUNS_CHANGED_EVENT));
  }
}

function useArmedStop(runId: string | undefined) {
  const [armed, setArmed] = useState(false);
  const [stopping, setStopping] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  // Disarm on run changes so confirmation cannot transfer to the wrong session.
  useEffect(() => {
    setArmed(false);
    return () => window.clearTimeout(timer.current);
  }, [runId]);

  async function activate(id: string) {
    if (!armed) {
      setArmed(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setArmed(false), ARMED_MS);
      return;
    }
    setStopping(true);
    await stopRun(id);
    setStopping(false);
    setArmed(false);
  }

  return {armed, stopping, activate};
}

function stopFace(armed: boolean): {
  label: string;
  tooltip: string;
  icon: IconName;
} {
  return armed
    ? {
        label: 'Confirm stop',
        tooltip: 'Stopping cannot be undone',
        icon: 'warning',
      }
    : {
        label: 'Stop run',
        tooltip: 'Stop this research session',
        icon: 'stop',
      };
}

// Stop belongs in the header because the same run is reachable from both chat
// and results.
export function CancelRunControl({
  runId,
  status,
}: {
  runId: string | undefined;
  status: RunStatus | undefined;
}) {
  const {armed, stopping, activate} = useArmedStop(runId);
  const [resume, setResume] = useState<{
    runId: string;
    status: RunStatus;
    canResume: boolean;
    requiresAck: boolean;
  } | null>(null);

  useEffect(() => {
    let current = true;
    if (!runId || (status !== 'paused' && status !== 'failed')) {
      setResume(null);
      return () => {
        current = false;
      };
    }
    setResume(null);
    void getRun(runId)
      .then(run => {
        if (!current) return;
        setResume({
          runId,
          status,
          canResume: run.can_resume === true,
          requiresAck: run.resume_requires_ack !== false,
        });
      })
      .catch(() => {
        if (current)
          setResume({runId, status, canResume: false, requiresAck: true});
      });
    return () => {
      current = false;
    };
  }, [runId, status]);

  if (!runId) return null;

  const canStop = isStoppableStatus(status) && status !== 'paused';
  const canResume =
    resume?.runId === runId && resume.status === status && resume.canResume;
  if (!canStop && !canResume) return null;

  const face = stopFace(armed);
  return (
    <>
      {canResume && resume && (
        <ResumeRunControl
          key={runId}
          runId={runId}
          requiresAck={resume.requiresAck}
          onResumed={() => window.location.reload()}
          compact
        />
      )}
      {canStop && (
        <Button
          variant="tonal"
          size="sm"
          icon={face.icon}
          tooltip={face.tooltip}
          tooltipPlacement="bottom"
          aria-label={face.label}
          disabled={stopping}
          onClick={() => void activate(runId)}
        >
          <span>{face.label}</span>
        </Button>
      )}
    </>
  );
}
