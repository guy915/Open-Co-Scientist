import type {RunWithSummary} from '@/shared/api/runs';
import {ResumeRunControl} from '@/shared/ui/resume_run';
import {Card} from '@/shared/ui';
import {ReportDocument} from './run_detail_shell';

function recoveryTitle(run: RunWithSummary): string {
  return run.resume_requires_ack === false ? 'Run paused' : 'Run interrupted';
}

function recoveryMessage(run: RunWithSummary): string {
  if (run.failure_kind === 'llm_credit_exhausted') {
    return 'The provider ran out of credits. Top up your provider account, then resume. Completed stages are kept. The rejected request was not accepted; retrying the unfinished stage may repeat earlier successful requests and charges.';
  }
  if (run.failure_kind === 'provider_admission') {
    return 'The provider rejected a request before accepting it. Resolve the provider issue, then resume. Your completed work is saved.';
  }
  if (run.resume_requires_ack !== false) {
    return 'A provider request was interrupted before its outcome could be confirmed. Your completed work is saved, and the request may need to be repeated.';
  }
  return 'The run paused at a saved checkpoint. Your completed work is saved and can be resumed.';
}

export function RunRecovery({
  run,
  onResumed,
}: {
  run: RunWithSummary;
  onResumed: () => void;
}) {
  return (
    <ReportDocument title={recoveryTitle(run)}>
      <Card tone="warning" layoutClassName="mb-6">
        <p className="m-0">{recoveryMessage(run)}</p>
      </Card>
      <ResumeRunControl
        runId={run.id}
        requiresAck={run.resume_requires_ack !== false}
        onResumed={onResumed}
      />
    </ReportDocument>
  );
}
