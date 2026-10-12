import {Card} from './card';

interface LaunchNoticeProps {
  message: string;
  resumesAt?: number | null;
  layoutClassName?: string;
  announce?: boolean;
}

export function LaunchNotice({
  message,
  resumesAt,
  layoutClassName,
  announce = true,
}: LaunchNoticeProps) {
  const date =
    resumesAt === null || resumesAt === undefined
      ? null
      : new Date(resumesAt * 1000);
  const returnTime =
    date && Number.isFinite(date.getTime()) && date.getTime() > Date.now()
      ? date
      : null;
  return (
    <Card
      tone="warning"
      role={announce ? 'status' : undefined}
      aria-live={announce ? 'polite' : undefined}
      aria-atomic={announce ? 'true' : undefined}
      layoutClassName={layoutClassName}
    >
      <p className="m-0 break-words">{message}</p>
      {returnTime ? (
        <p className="m-0 mt-1">
          Expected back{' '}
          <time dateTime={returnTime.toISOString()}>
            {returnTime.toLocaleString(undefined, {
              year: 'numeric',
              month: 'short',
              day: 'numeric',
              hour: 'numeric',
              minute: '2-digit',
              timeZoneName: 'short',
            })}
          </time>
          .
        </p>
      ) : (
        <p className="m-0 mt-1">
          We will update this notice when service returns.
        </p>
      )}
    </Card>
  );
}
