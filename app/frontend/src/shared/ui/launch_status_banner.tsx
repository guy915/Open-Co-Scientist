import {useLaunchStatus} from '@/shared/hooks/launch_status_context';
import {LaunchNotice} from './launch_notice';

export const LAUNCH_NOTICE_ID = 'launch-availability-notice';

export function LaunchStatusBanner() {
  const {status} = useLaunchStatus();
  return (
    <div
      id={LAUNCH_NOTICE_ID}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      {status?.message && (
        <LaunchNotice
          message={status.message}
          resumesAt={status.resumes_at}
          announce={false}
          layoutClassName="mx-4 mb-3 phone:mx-3"
        />
      )}
    </div>
  );
}
